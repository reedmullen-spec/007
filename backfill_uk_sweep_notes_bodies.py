"""Writes the two fields import_scored_csv.py has no path for onto the rows
it already created from the UK all-regions sweep: the Notes property, and
each record's markdown write-up as page content.

Run after the CSV import, against the same JSON, with the same source tag
the import used (the notice IDs it generated are what this looks up):

    python backfill_uk_sweep_notes_bodies.py data/uk-all-regions-records.json --dry-run
    python backfill_uk_sweep_notes_bodies.py data/uk-all-regions-records.json

Not UK-only despite the name — any location sweep exported in the same
{"records": [{"properties": ..., "body": ...}]} shape works, given its
tag: the German all-states sweep runs as

    python backfill_uk_sweep_notes_bodies.py \
        data/germany-all-states-records.json --source-tag DESWEEP

Two deliberate departures from the shared helpers, both local to this
script so enrich.py's pack path is untouched:

  - Notes is written as several rich_text segments rather than through
    NotionClient._rt(), which truncates at MAX_BLOCK_CHARS. Seven of the
    109 notes run past that (longest 2297 chars) and would lose their tail
    silently. Notion's limit is per segment, not per property.
  - the body goes through notion_client.markdown_to_blocks(), which now
    renders inline bold, code and links plus real tables. This script
    originally carried its own converter because the shared one dropped all
    of that; that gap has since been fixed in place for enrich.py's packs
    too, so the duplicate is gone.

Idempotent: a row that already has page content is left alone, so a
re-run after a partial failure never appends a second copy of the body.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from src.config import env, load_config
from src.notion_client import BASE, MAX_BLOCK_CHARS, NotionClient, markdown_to_blocks

DEFAULT_SOURCE_TAG = "UKSWEEP"


def notice_id(name: str, source_tag: str) -> str:
    """Same slug rule import_scored_csv.py used to create these rows."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return f"{source_tag}:{slug}"


def notes_prop(text: str) -> dict:
    """Notes as segments, so nothing past MAX_BLOCK_CHARS is dropped."""
    return {"rich_text": [{"type": "text", "text": {"content": text[i:i + MAX_BLOCK_CHARS]}}
                          for i in range(0, len(text), MAX_BLOCK_CHARS)] or
                         [{"type": "text", "text": {"content": ""}}]}


def has_content(notion: NotionClient, page_id: str) -> bool:
    data = notion._check(notion.session.get(
        f"{BASE}/blocks/{page_id}/children", params={"page_size": 1}, timeout=30))
    return bool(data.get("results"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path")
    parser.add_argument("--source-tag", default=DEFAULT_SOURCE_TAG,
                        help="notice_id prefix the CSV import used for these "
                             "rows — must match or nothing will be found")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    records = json.load(open(args.json_path, encoding="utf-8"))["records"]
    print(f"{len(records)} records in {args.json_path}")

    notes_done = body_done = body_skipped = missing = failed = 0
    for i, rec in enumerate(records, 1):
        props = rec["properties"]
        name = (props.get("Name") or "").strip()
        nid = notice_id(name, args.source_tag)

        row = notion.find_row(nid)
        if not row:
            print(f"  [{i}/{len(records)}] MISSING {nid}", file=sys.stderr)
            missing += 1
            continue

        page_id = row["id"]
        note = (props.get("Notes") or "").strip()
        body = (rec.get("body") or "").strip()
        blocks = markdown_to_blocks(body)
        print(f"  [{i}/{len(records)}] {name[:66]} "
              f"(notes {len(note)}c, {len(blocks)} blocks)")
        if args.dry_run:
            if note:
                notes_done += 1
            if body:
                body_done += 1
            continue

        try:
            if note:
                notion.update_properties(page_id, {"Notes": notes_prop(note)})
                notes_done += 1
            if body:
                if has_content(notion, page_id):
                    body_skipped += 1
                else:
                    for j in range(0, len(blocks), 100):
                        notion._check(notion.session.patch(
                            f"{BASE}/blocks/{page_id}/children",
                            json={"children": blocks[j:j + 100]}, timeout=60))
                    body_done += 1
        except Exception as exc:
            print(f"    WARNING: {name[:50]}: {exc}", file=sys.stderr)
            failed += 1
            continue
        time.sleep(0.4)

    verb = "Would write" if args.dry_run else "Wrote"
    print(f"{verb} {notes_done} notes and {body_done} bodies — "
          f"{body_skipped} bodies skipped (page already had content), "
          f"{missing} rows not found, {failed} failed")
    return 1 if (missing or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
