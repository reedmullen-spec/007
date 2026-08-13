"""One-off: consolidates the master schema's two separate contractor
fields — 'General contractor' and 'JV / parents' — into the single
'General contractor/JV' property. Both source properties are being
dropped from the schema after this runs (see CLAUDE.md); every row's
existing 'General contractor/JV' text is preserved and only extended
with whatever 'General contractor'/'JV / parents' contribute that isn't
already reflected in it.

Usage:
    python migrate_gc_jv.py               # live
    python migrate_gc_jv.py --dry-run     # print what would change
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def merged_text(existing_gcjv: str, gc: str, jv: str) -> str:
    """existing_gcjv is kept as the base (it's often the more detailed,
    manually-researched text — see CLAUDE.md); gc/jv only contribute
    pieces not already present in it."""
    parts = [existing_gcjv.strip()] if existing_gcjv.strip() else []
    gc = gc.strip()
    if gc and gc not in existing_gcjv:
        parts.append(gc)
    jv = jv.strip()
    if jv and jv not in existing_gcjv and jv not in gc:
        parts.append(f"JV: {jv}")
    # dedupe while preserving order (existing_gcjv and gc are sometimes
    # byte-identical — the sample data showed this for ~2/3 of overlaps)
    seen: set[str] = set()
    deduped = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return " | ".join(deduped)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({})
    print(f"Scanning {len(rows)} rows")

    changed = 0
    for row in rows:
        gc = _prop_rt(row, "General contractor")
        jv = _prop_rt(row, "JV / parents")
        gcjv = _prop_rt(row, "General contractor/JV")
        if not gc and not jv:
            continue

        new_value = merged_text(gcjv, gc, jv)
        if new_value == gcjv:
            continue

        title = notion.row_title(row)
        changed += 1
        print(f"  {title[:70]}")
        print(f"    old GC/JV: {gcjv!r}")
        print(f"    GC: {gc!r} | JV: {jv!r}")
        print(f"    new GC/JV: {new_value!r}")
        if not args.dry_run:
            notion.update_properties(row["id"], {
                "General contractor/JV": NotionClient._rt(new_value)})
            time.sleep(0.4)

    verb = "Would update" if args.dry_run else "Updated"
    print(f"{verb} {changed} of {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
