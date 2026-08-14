"""One-off, re-runnable: fixes rows whose Value is stored in millions
instead of raw dollars — a recurring data-entry pattern (a human or a
model naturally types "400" to mean "€400m"), not tied to one source.
First found via scope_check.py (Source=NEWS, blank Notice ID, ~32 rows)
while investigating why real megaprojects were scoring "value below the
5,000,000 floor" in a full-database rescore dry-run. Recurred Aug 2026
for 46 Source=MANUAL rows (hand-researched — Ontario Line, Darlington
SMR, PORR, Bechtel, etc.), fixed by hand that time; generalized here so
the next occurrence, on any source, is one command.

Targets any row matching the signature (0 < Value < --max-value) and
multiplies Value by 1e6. Recomputes Value band alongside it, since that's
mechanical. Deliberately does NOT touch Fit/Status/Fit reason/Fit
dimensions here — scoring is a separate, deterministic step; run
rescore_existing.py (or the equivalent for a specific batch) after this,
not before, or the rescore will still see the wrong Value.

Usage:
    python fix_value_units.py               # live, scans every row
    python fix_value_units.py --dry-run     # print what would change
    python fix_value_units.py --source MANUAL   # scope to one Source
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
    parser.add_argument("--source", default="",
                        help="Restrict to one Source (e.g. MANUAL, NEWS). "
                             "Default: scan every row regardless of source.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    filter_obj = ({"property": "Source", "select": {"equals": args.source}}
                  if args.source else {})
    rows = notion.query_all_rows(filter_obj)
    print(f"Scanning {len(rows)} rows" + (f" (Source={args.source})" if args.source else ""))

    fixed = 0
    for row in rows:
        props = row.get("properties") or {}
        value = (props.get("Value") or {}).get("number")
        if value is None or not (0 < value < args.max_value):
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
