"""Retroactive cleanup — disqualifies existing "New" rows whose Value is
known and below the current filters.min_value_* threshold (all four
currencies are set to the same number, so no currency-aware branching is
needed here). Rows with no disclosed value are left alone regardless —
keep_unknown_value applies at ingest time and isn't being revisited here.

Only "New" rows are touched, not "This week"/"Active Contact"/etc. — a
filter threshold change shouldn't retroactively disqualify something an
AE is already working.

Usage:
    python cleanup_low_value.py               # live
    python cleanup_low_value.py --dry-run     # print what would be disqualified
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient


def _prop_number(row: dict, name: str):
    return ((row.get("properties") or {}).get(name) or {}).get("number")


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-value", type=float, default=None,
                        help="Override the threshold (defaults to filters.min_value_eur)")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    floor = args.min_value if args.min_value is not None else cfg["filters"]["min_value_eur"]

    rows = notion.query_all_rows({"property": "Status", "select": {"equals": "New"}})
    print(f"Scanning {len(rows)} 'New' rows against a {floor:,.0f} floor")

    flagged = 0
    for row in rows:
        value = _prop_number(row, "Value")
        if value is None or value >= floor:
            continue
        title = notion.row_title(row)
        print(f"  {title[:80]} — {value:,.0f} < {floor:,.0f}")
        flagged += 1
        if not args.dry_run:
            note = f"Auto-disqualified: value {value:,.0f} below the {floor:,.0f} floor"
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
