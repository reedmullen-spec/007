"""One-off: backfills the SDR field on existing rows from their
already-assigned AE, via routing.ae_sdr_map. AE is set once at ingest
time and never changes afterward, so this only needs to run once for the
historical backlog — ingest.py writes SDR directly for every row from now on.

Usage:
    python backfill_sdr.py               # live
    python backfill_sdr.py --dry-run     # print what would change
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    ae_sdr_map = cfg["routing"].get("ae_sdr_map", {})

    # ensure_schema() normally runs inside ingest.py's live path — this
    # script needs to be able to run standalone regardless of whether a
    # live ingest has happened since SDR was added to the schema.
    if not args.dry_run:
        notion.ensure_schema()

    rows = notion.query_all_rows({})
    print(f"Scanning {len(rows)} rows")

    changed = 0
    for row in rows:
        ae = _prop_select(row, "AE")
        sdr = ae_sdr_map.get(ae, "")
        if not sdr or sdr == _prop_select(row, "SDR"):
            continue
        title = notion.row_title(row)
        print(f"  {title[:70]} — AE={ae} -> SDR={sdr}")
        changed += 1
        if not args.dry_run:
            notion.update_properties(row["id"], {"SDR": {"select": {"name": sdr}}})
            time.sleep(0.4)

    verb = "Would set" if args.dry_run else "Set"
    print(f"{verb} SDR on {changed} of {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
