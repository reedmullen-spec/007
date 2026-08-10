"""007 nightly sync — pull AE-owned edits back to the master database.

The AE owns Status / Next action / Next action date / Notes / Outcome /
Correction needed — all pushed to the master here, every night, always
(the master never writes any of these itself, so there's nothing to
fight over). A Correction needed entry is ALSO collected and DM'd to Reed
— written to the master for the record, but still surfaced directly since
it usually means some other field needs a human's attention, not just
this one.

The master owns the facts (Fit/GC/Location/Expected concrete
start/Expected completion/Value band), mirrored onto the AE's page only
at Monday triage — except when a person edits one of those fields
directly on their own page. That edit is authoritative: sync_fact_drift()
(src/ae_pages.py) detects it against a snapshot of what was last mirrored
and pushes it to the master immediately, so it doesn't have to wait for
next Monday to take effect, and next Monday's mirror won't stomp it.

Usage:
    python sync.py               # live
    python sync.py --dry-run     # print what would change, write nothing
"""
from __future__ import annotations

import argparse

from src import state
from src.ae_pages import ensure_ae_database, sync_fact_drift
from src.config import env, load_config
from src.notion_client import NotionClient
from src.slack_client import SlackClient

AE_NAMES = [a for a in NotionClient.AES if a != "unassigned"]
EPOCH = "2020-01-01T00:00:00.000Z"


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def _prop_sel(row: dict, name: str) -> str:
    return (((row.get("properties") or {}).get(name) or {}).get("select") or {}).get("name", "")


def _prop_date(row: dict, name: str) -> str:
    d = ((row.get("properties") or {}).get(name) or {}).get("date") or {}
    return d.get("start") or ""


def _prop_url(row: dict, name: str) -> str:
    return (row.get("properties") or {}).get(name, {}).get("url") or ""


def _prop_title(row: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in
                  (row.get("properties") or {}).get(name, {}).get("title", []))


def _master_id_from_url(url: str) -> str:
    """Notion page URLs end in the page's 32-char id, often prefixed by a
    title slug and dashed or not — stripping all dashes leaves the id as
    exactly the last 32 characters, regardless of which form it's in."""
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return tail.replace("-", "")[-32:]


def sync_person(notion: NotionClient, person: str, sync_state: dict,
                corrections: list, dry_run: bool) -> int:
    db_id = ensure_ae_database(notion, person)
    last_sync = sync_state.setdefault("last_sync", {}).get(person, EPOCH)
    notified = sync_state.setdefault("notified_corrections", [])

    rows = notion.query_rows(
        {"timestamp": "last_edited_time", "last_edited_time": {"after": last_sync}},
        database_id=db_id, limit=100)
    if not rows:
        return 0

    newest = max((r.get("last_edited_time", "") for r in rows), default=last_sync)
    for row in rows:
        master_url = _prop_url(row, "Master row")
        if not master_url:
            continue
        master_id = _master_id_from_url(master_url)
        status = _prop_sel(row, "Status")
        next_action = _prop_rt(row, "Next action")
        next_action_date = _prop_date(row, "Next action date")
        notes = _prop_rt(row, "Notes")
        outcome = _prop_sel(row, "Outcome")
        correction = _prop_rt(row, "Correction needed")

        props = {}
        if status:
            props["Status"] = {"select": {"name": status}}
        if next_action:
            props["Next action"] = NotionClient._rt(next_action)
        if next_action_date:
            props["Next action date"] = {"date": {"start": next_action_date}}
            if status == "Recontact later":
                # AE's Next action date doubles as the master's Recontact
                # date — one field for the AE to fill in, not two.
                props["Recontact date"] = {"date": {"start": next_action_date}}
        if notes:
            props["Notes"] = NotionClient._rt(notes)
        if outcome:
            props["Outcome"] = {"select": {"name": outcome}}
        if correction:
            props["Correction needed"] = NotionClient._rt(correction)

        print(f"[{person}] {_prop_title(row, 'Project')[:60]!r} -> "
              f"status={status or '-'} next_action_date={next_action_date or '-'}")
        if props and not dry_run:
            notion.update_properties(master_id, props)
            # Facts (Fit/GC/Location/etc.) are separate from the props
            # above — pushed only when the person actually edited one on
            # their own page, detected against the last-mirrored snapshot.
            sync_fact_drift(notion, row, master_id)

        if correction and row["id"] not in notified:
            corrections.append({"person": person, "project": _prop_title(row, "Project"),
                                "text": correction, "url": row.get("url", "")})
            notified.append(row["id"])

    if not dry_run:
        sync_state["last_sync"][person] = newest
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    sync_state = state.load("sync")
    corrections: list[dict] = []

    total = 0
    for person in AE_NAMES:
        total += sync_person(notion, person, sync_state, corrections, args.dry_run)

    if corrections:
        lines = [f"• *{c['person']}* — <{c['url']}|{c['project'][:60]}>: {c['text']}"
                 for c in corrections]
        text = "*Corrections flagged overnight:*\n" + "\n".join(lines)
        print(text)
        if not args.dry_run:
            slack = SlackClient(env("SLACK_BOT_TOKEN"))
            slack._call("chat.postMessage",
                        json={"channel": cfg["slack"]["approver_user_id"],
                              "text": text, "unfurl_links": False})

    if not args.dry_run:
        state.save("sync", sync_state)
    print(f"Synced {total} edited rows, {len(corrections)} new corrections flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
