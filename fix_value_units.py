"""One-off: fixes the ~32 rows (all Source=NEWS, all blank Notice ID)
whose Value is stored in millions instead of raw dollars — found via
scope_check.py while investigating why real megaprojects (JFK Terminal,
Hudson Tunnel, Second Avenue Subway, NYC jails, etc.) were scoring
"value below the 5,000,000 floor" in a full-database rescore dry-run.

Targets exactly the rows matching that signature (Source=NEWS, notice_id
empty, 0 < Value < --max-value) and multiplies Value by 1e6. Deliberately
does NOT touch Fit/Status here — these rows' current Fit was computed
correctly back when the value was presumably entered right, and a
subsequent rescore (once Value is fixed) will re-derive Value band
correctly too. Run this BEFORE any rescore_existing.py pass, or the
rescore will wrongly disqualify all of them.

Usage:
    python fix_value_units.py               # live
    python fix_value_units.py --dry-run     # print what would change
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-value", type=float, default=100_000,
                        help="Only fix rows with a stored Value below this "
                             "(a real raw-dollar value this low never "
                             "happens in this system's domain)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    nid_prop = cfg["notion"]["notice_id_property"]

    rows = notion.query_all_rows({"property": "Source", "select": {"equals": "NEWS"}})
    print(f"Scanning {len(rows)} Source=NEWS rows")

    fixed = 0
    for row in rows:
        props = row.get("properties") or {}
        nid_vals = (props.get(nid_prop) or {}).get("rich_text", [])
        notice_id = nid_vals[0].get("plain_text", "") if nid_vals else ""
        value = (props.get("Value") or {}).get("number")
        if notice_id or value is None or not (0 < value < args.max_value):
            continue

        title = "".join(t.get("plain_text", "") for t in
                        (props.get("Name") or {}).get("title", []))
        new_value = value * 1_000_000
        band = ("Under 50M" if new_value < 50_000_000
                else "50-250M" if new_value < 250_000_000 else "250M+")
        print(f"  {title[:70]}: {value} -> {new_value:,.0f} (band -> {band})")
        fixed += 1

        if not args.dry_run:
            notion.update_properties(row["id"], {
                "Value": {"number": new_value},
                "Value band": {"select": {"name": band}},
            })
            time.sleep(0.4)

    verb = "Would fix" if args.dry_run else "Fixed"
    print(f"{verb} {fixed} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
