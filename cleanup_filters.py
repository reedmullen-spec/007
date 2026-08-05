"""Retroactive cleanup — re-checks existing "New" tender rows against the
current relevance filter and disqualifies the ones that no longer pass,
without deleting anything (never delete rows — see CLAUDE.md).

Only checks TED/FTS/AUSTENDER/SAM rows (the ones filter_projects() gates).
NEWS uses a separate keyword pipeline (news.py) and MANUAL rows are
hand-added by the team, so both are left untouched here.

A row's original CPV/UNSPSC/NAICS codes aren't stored on the Notion page,
so this can only re-check the keyword half of filter_projects()'s
code-AND-keyword test — but that's enough to be conclusive: any row
missing an include-keyword hit (or hitting an exclude keyword) would fail
the current filter regardless of its code, and can be safely flagged.
Rows that DO have a keyword hit are left alone even though their code
match can't be re-verified — erring toward not touching real data.

Usage:
    python cleanup_filters.py               # live
    python cleanup_filters.py --dry-run     # print what would be disqualified
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.filtering import _keyword_hit
from src.notion_client import NotionClient

CHECKED_SOURCES = {"TED", "FTS", "AUSTENDER", "SAM"}


def _prop_select(row: dict, name: str) -> str:
    return ((row.get("properties") or {}).get(name) or {}).get("select", {}).get("name", "")


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def should_disqualify(row: dict, cfg: dict) -> str | None:
    """Returns a disqualify reason if this row would fail the tightened
    filter, else None."""
    if _prop_select(row, "Source") not in CHECKED_SOURCES:
        return None
    title = "".join(t.get("plain_text", "") for t in
                    ((row.get("properties") or {}).get("Name") or {}).get("title", []))
    f = cfg["filters"]
    if _keyword_hit(title, f.get("exclude_keywords", [])):
        return "matches an exclude keyword"
    if not _keyword_hit(title, f["include_keywords"]):
        return "no include-keyword hit in title"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({"property": "Status", "select": {"equals": "New"}})
    print(f"Scanning {len(rows)} 'New' rows")

    flagged = 0
    for row in rows:
        reason = should_disqualify(row, cfg)
        if not reason:
            continue
        title = notion.row_title(row)
        print(f"  [{_prop_select(row, 'Source')}] {title[:80]} — {reason}")
        flagged += 1
        if not args.dry_run:
            note = f"Auto-disqualified (retroactive filter cleanup): {reason}"
            existing = _prop_rt(row, "Fit reason")
            combined = f"{existing} · {note}" if existing else note
            notion.update_properties(row["id"], {
                "Status": {"select": {"name": "Disqualified"}},
                "Fit reason": NotionClient._rt(combined),
            })
            time.sleep(0.4)

    verb = "Would disqualify" if args.dry_run else "Disqualified"
    print(f"{verb} {flagged} of {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
