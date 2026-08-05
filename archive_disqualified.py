"""One-off: archives every row currently Status=Disqualified.

Requested as a one-time cleanup after cleanup_filters.py flagged ~352 rows
as noise (loose CPV-code matches with no real concrete relevance) — rather
than leave them sitting in the database, archive them outright. Uses
Notion's `archived: true` patch, which is reversible from Notion's trash
(not a hard delete), since even a requested one-off shouldn't be
irreversible if something's wrong.

Usage:
    python archive_disqualified.py               # live
    python archive_disqualified.py --dry-run     # print what would be archived
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient

BASE = "https://api.notion.com/v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({"property": "Status", "select": {"equals": "Disqualified"}})
    print(f"Found {len(rows)} Disqualified rows")

    archived = 0
    for row in rows:
        title = notion.row_title(row)
        print(f"  {title[:80]}")
        if not args.dry_run:
            notion._check(notion.session.patch(
                f"{BASE}/pages/{row['id']}", json={"archived": True}, timeout=30))
            archived += 1
            time.sleep(0.4)

    verb = "Would archive" if args.dry_run else "Archived"
    print(f"{verb} {len(rows) if args.dry_run else archived} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
