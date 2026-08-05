"""007 Monday triage — the week's projects, per region.

Reads the Notion database, picks the top-N "New" projects per region
(manually-added intel first, then fit, then value), flips them to
"This week", and posts one Slack message per region with links to the
Notion rows. Unfinished "This week" rows from last week stay in the list
(one in, one out): they count toward the cap and are re-listed.

Usage:
    python triage.py               # live
    python triage.py --dry-run     # print the picks, change nothing
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient
from src.slack_client import SlackClient

FIT_ORDER = {"high": 0, "medium": 1, "low": 2}


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def _prop_number(row: dict, name: str):
    return ((row.get("properties") or {}).get(name) or {}).get("number")


def rank_key(row: dict, manual_first: bool):
    manual = 0 if (manual_first and _prop_select(row, "Source") == "MANUAL") else 1
    fit = FIT_ORDER.get(_prop_select(row, "Fit"), 1)
    value = _prop_number(row, "Value") or 0
    return (manual, fit, -value)


def pick_for_region(notion: NotionClient, region: str, n: int,
                    manual_first: bool) -> tuple[list[dict], list[dict]]:
    """Returns (carryover This-week rows, new picks) totalling <= n."""
    carry = notion.query_rows({"and": [
        {"property": "Region", "select": {"equals": region}},
        {"property": "Status", "select": {"equals": "This week"}},
    ]})
    slots = max(n - len(carry), 0)
    fresh = notion.query_rows({"and": [
        {"property": "Region", "select": {"equals": region}},
        {"property": "Status", "select": {"equals": "New"}},
    ]})
    fresh.sort(key=lambda r: rank_key(r, manual_first))
    return carry, fresh[:slots]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    slack = None if args.dry_run else SlackClient(env("SLACK_BOT_TOKEN"))

    channels = cfg["slack"]["news_channels"]
    ae_slack = cfg["slack"].get("ae_slack_ids", {})
    region_mentions = cfg["slack"].get("region_mentions", {})
    manual_first = cfg["triage"].get("manual_first", True)

    week = time.strftime("%d %b %Y")
    total_new = 0

    for region, n in cfg["triage"]["per_region"].items():
        carry, picks = pick_for_region(notion, region, n, manual_first)
        if not carry and not picks:
            continue

        lines = []
        for tag, rows in (("carried over", carry), ("new", picks)):
            for row in rows:
                title = notion.row_title(row)
                fit = _prop_select(row, "Fit")
                gc = ""
                rich = ((row.get("properties") or {}).get("General contractor")
                        or {}).get("rich_text", [])
                if rich:
                    gc = rich[0].get("plain_text", "")
                url = row.get("url", "")
                extra = f" · {gc}" if gc else ""
                mark = " (carried over)" if tag == "carried over" else ""
                src = " · from the team" if _prop_select(row, "Source") == "MANUAL" else ""
                lines.append(f"• <{url}|{title[:80]}> — {fit} fit{extra}{src}{mark}")

        print(f"[{region}] {len(carry)} carried, {len(picks)} new")
        if args.dry_run:
            for line in lines:
                print("   " + line)
            continue

        for row in picks:
            notion.update_properties(row["id"], {
                "Status": {"select": {"name": "This week"}}})
            time.sleep(0.4)
        total_new += len(picks)

        raw = channels.get(region) or channels.get(region.split("_")[0])
        targets = raw if isinstance(raw, list) else [raw]
        names = region_mentions.get(region, [])
        mention_txt = " ".join(f"<@{ae_slack[m]}>" for m in names
                               if ae_slack.get(m) and not ae_slack[m].startswith("TODO"))
        header = f"*Your projects for the week — {region.replace('_', ' ').upper()} · w/c {week}*"
        body = header + ("\n" + mention_txt if mention_txt else "") + "\n" + "\n".join(lines) \
            + "\n_Update the Status in Notion as you work them — done, disqualified, or recontact later._"
        for channel in targets:
            if channel and not str(channel).startswith("TODO"):
                slack._call("chat.postMessage",
                            json={"channel": channel, "text": body,
                                  "unfurl_links": False})
                time.sleep(1)

    print(f"Marked {total_new} projects as This week")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
