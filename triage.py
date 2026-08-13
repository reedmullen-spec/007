"""007 Monday triage — the week's projects, per person.

Reads the Notion database, picks the top-N "New" projects per triage list
(manually-added intel first, then fit, then value), flips them to "This
week", and posts one Slack message per list with links to the Notion rows.
Unfinished "This week" rows from last week stay in the list (one in, one
out): they count toward the cap and are re-listed. "Recontact later" rows
no longer auto-resurface (Recontact date was removed) — a human moves them
back to "New" or "This week" manually when ready.

One list = one person's page (config `triage.lists`) — filters can match on
AE, Region, SDR (derived from AE via routing.ae_sdr_map — Alex/Jamie's own
lists use this to cover their whole patch of AEs in one list), or Product
fit, so the same project can legitimately appear on more than one person's
list (e.g. the SDR squad works the same projects as the White Cap AEs from
a different angle). Lists are never de-duplicated against each other.

Usage:
    python triage.py               # live
    python triage.py --dry-run     # print the picks, change nothing
"""
from __future__ import annotations

import argparse
import time

from src.ae_pages import upsert_ae_row
from src.config import env, load_config
from src.focus import get_focus, mark_applied, rank_with_focus
from src.notion_client import NotionClient
from src.slack_client import SlackClient

FIT_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Disqualified": 3}


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def _prop_number(row: dict, name: str):
    return ((row.get("properties") or {}).get(name) or {}).get("number")


def _prop_date(row: dict, name: str) -> str:
    d = ((row.get("properties") or {}).get(name) or {}).get("date") or {}
    return d.get("start") or ""


def rank_key(row: dict, manual_first: bool):
    manual = 0 if (manual_first and _prop_select(row, "Source") == "MANUAL") else 1
    fit = FIT_ORDER.get(_prop_select(row, "Fit"), 1)
    value = _prop_number(row, "Value") or 0
    return (manual, fit, -value)


def build_filter(filter_spec: dict) -> dict:
    """Translate a triage.lists filter spec into a Notion filter object.
    Supports `AE equals`, `Region equals`, `SDR equals`, and
    `Product fit contains`."""
    conds = []
    for key, value in filter_spec.items():
        if key == "Product fit contains":
            conds.append({"property": "Product fit", "multi_select": {"contains": value}})
        elif key in ("AE", "Region", "SDR"):
            conds.append({"property": key, "select": {"equals": value}})
        else:
            raise ValueError(f"Unsupported triage filter key: {key!r}")
    return conds[0] if len(conds) == 1 else {"and": conds}


def gather_for_list(notion: NotionClient, list_cfg: dict) -> tuple[list[dict], list[dict]]:
    """Returns (carryover rows, fresh 'New' candidates), unranked.
    Carryover = still "This week". "Recontact later" rows do NOT carry over
    — there's no Recontact date to resurface them by anymore, so they stay
    parked until a human moves them back to "New" or "This week" by hand.

    Notion's filter API only allows compound and/or nesting two levels
    deep, so `base` (a leaf condition for every list.filter in current
    config) is distributed into each branch of the "or" rather than
    wrapping the whole "or" in an outer "and" — that would be three levels
    and Notion 400s on it."""
    base = build_filter(list_cfg["filter"])
    # Fit=Disqualified means never, per the scorer's design — excluded from
    # every branch, not just fresh picks. A row can be Status="This week"
    # from an earlier run and only later get rescored to Disqualified (e.g.
    # a retroactive rescore_existing.py pass); without this check here it
    # would keep carrying over and re-triaging every week regardless (real
    # incident: a rescored-Disqualified row reached a person via Slack).
    not_dq = {"property": "Fit", "select": {"does_not_equal": "Disqualified"}}
    carry = notion.query_rows({"and": [base, not_dq,
                              {"property": "Status", "select": {"equals": "This week"}}]})
    fresh = notion.query_rows({"and": [base, not_dq,
                              {"property": "Status", "select": {"equals": "New"}}]})
    return carry, fresh


def _row_line(notion: NotionClient, row: dict, tag: str, ae_url: str = "") -> str:
    title = notion.row_title(row)
    fit = _prop_select(row, "Fit")
    rich = ((row.get("properties") or {}).get("General contractor/JV") or {}).get("rich_text", [])
    gc = rich[0].get("plain_text", "") if rich else ""
    url = row.get("url", "")
    extra = f" · {gc}" if gc else ""
    mark = " (carried over)" if tag == "carried over" else ""
    src = " · from the team" if _prop_select(row, "Source") == "MANUAL" else ""
    my_page = f" · <{ae_url}|my page>" if ae_url else ""
    return f"• <{url}|{title[:80]}> — {fit} fit{extra}{src}{mark}{my_page}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    slack = None if args.dry_run else SlackClient(env("SLACK_BOT_TOKEN"))
    api_key = env("ANTHROPIC_API_KEY") if not args.dry_run else ""

    channels = cfg["slack"]["news_channels"]
    ae_slack = cfg["slack"].get("ae_slack_ids", {})
    manual_first = cfg["triage"].get("manual_first", True)

    week = time.strftime("%d %b %Y")
    total_new = 0

    for list_cfg in cfg["triage"]["lists"]:
        name = list_cfg["name"]
        carry, fresh = gather_for_list(notion, list_cfg)
        slots = max(list_cfg["count"] - len(carry), 0)

        reasons: dict[str, str] = {}
        note = ""
        focus = get_focus(notion, name, cfg)   # cheap Notion read only
        if focus and not args.dry_run:
            ranked = rank_with_focus(api_key, cfg, focus["text"], fresh, slots)
            picks = [p["row"] for p in ranked]
            for p in ranked:
                reasons[p["row"]["id"]] = p["reason"]
            if len(picks) < slots and fresh:
                note = (f"_Only {len(picks)} matched this week's focus "
                        f"(\"{focus['text'][:60]}\")._")
        else:
            picks = sorted(fresh, key=lambda r: rank_key(r, manual_first))[:slots]
            for row in picks:
                reasons[row["id"]] = "default ranking"
            if focus and args.dry_run:
                print(f"[{name}] focus present but not applied in dry-run: "
                      f"{focus['text'][:80]!r}")

        if not carry and not picks:
            continue

        print(f"[{name}] {len(carry)} carried, {len(picks)} new")
        if args.dry_run:
            for tag, rows in (("carried over", carry), ("new", picks)):
                for row in rows:
                    print("   " + _row_line(notion, row, tag))
            continue

        for row in picks:
            notion.update_properties(row["id"], {
                "Status": {"select": {"name": "This week"}}})
            time.sleep(0.4)
        # Resurfaced "Recontact later" rows need flipping too; "This week"
        # carry rows are already in the right state.
        for row in carry:
            if _prop_select(row, "Status") != "This week":
                notion.update_properties(row["id"], {
                    "Status": {"select": {"name": "This week"}}})
                time.sleep(0.4)
            reasons.setdefault(row["id"], "carried over")
        total_new += len(picks)

        # Mirror this week's facts onto the person's own editable page, and
        # capture its URL so the Slack line can link straight to it
        # alongside the master row — built after this, not before, since
        # only a live run actually creates/updates that page.
        ae_urls: dict[str, str] = {}
        for row in carry + picks:
            ae_urls[row["id"]] = upsert_ae_row(
                notion, name, row, reasons.get(row["id"], "default ranking"))
            time.sleep(0.4)

        lines = []
        for tag, rows in (("carried over", carry), ("new", picks)):
            for row in rows:
                lines.append(_row_line(notion, row, tag, ae_urls.get(row["id"], "")))

        raw = channels.get(list_cfg["channel"])
        targets = raw if isinstance(raw, list) else [raw]
        mention_id = ae_slack.get(name, "")
        mention_txt = f"<@{mention_id}>" if mention_id and not mention_id.startswith("TODO") else ""
        header = f"*Your projects for the week — {name.title()} · w/c {week}*"
        body = header + ("\n" + mention_txt if mention_txt else "") + "\n" + "\n".join(lines)
        if note:
            body += "\n" + note
        body += "\n_Update the Status in Notion as you work them — done, disqualified, or recontact later._"
        for channel in targets:
            if channel and not str(channel).startswith("TODO"):
                slack._call("chat.postMessage",
                            json={"channel": channel, "text": body,
                                  "unfurl_links": False})
                time.sleep(1)

        if focus and not args.dry_run:
            mark_applied(notion, focus["row_id"])

    print(f"Marked {total_new} projects as This week")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
