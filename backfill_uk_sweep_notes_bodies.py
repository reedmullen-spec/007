"""Writes the two fields import_scored_csv.py has no path for onto the rows
it already created from the UK all-regions sweep: the Notes property, and
each record's markdown write-up as page content.

Run after the CSV import (source tag UKSWEEP), against the same JSON:

    python backfill_uk_sweep_notes_bodies.py data/uk-all-regions-records.json --dry-run
    python backfill_uk_sweep_notes_bodies.py data/uk-all-regions-records.json

Two deliberate departures from the shared helpers, both local to this
script so enrich.py's pack path is untouched:

  - Notes is written as several rich_text segments rather than through
    NotionClient._rt(), which truncates at MAX_BLOCK_CHARS. Seven of the
    109 notes run past that (longest 2297 chars) and would lose their tail
    silently. Notion's limit is per segment, not per property.
  - _markdown_to_blocks() handles headings, bullets and paragraphs but not
    inline bold or tables, so it would render "**October 2026**" with the
    asterisks showing on 104 of the records and flatten 7 markdown tables
    into pipe-delimited paragraphs. The converter here emits real bold
    annotations and real table blocks. (enrich.py's packs have the same
    limitation; not changed here because that is shared, load-bearing code
    and this is a one-off.)

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
from src.notion_client import BASE, MAX_BLOCK_CHARS, NotionClient

SOURCE_TAG = "UKSWEEP"


def notice_id(name: str) -> str:
    """Same slug rule import_scored_csv.py used to create these rows."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return f"{SOURCE_TAG}:{slug}"


ESCAPED_STAR = "\x00STAR\x00"


def rich(text: str) -> list[dict]:
    """Inline markdown -> rich_text, honouring **bold** spans.

    A backslash-escaped asterisk is parked behind a sentinel first: the
    NABERS ratings in this sweep are written "5-5.5\\*", and leaving that
    asterisk in place both breaks the bold split and leaves the backslash
    showing in Notion."""
    text = text.replace("\\*", ESCAPED_STAR)
    out: list[dict] = []
    for part in re.split(r"(\*\*[^*]+\*\*)", text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        content = part[2:-2] if bold else part
        content = content.replace(ESCAPED_STAR, "*")
        for i in range(0, len(content), MAX_BLOCK_CHARS):
            chunk = content[i:i + MAX_BLOCK_CHARS]
            seg: dict = {"type": "text", "text": {"content": chunk}}
            if bold:
                seg["annotations"] = {"bold": True}
            out.append(seg)
    return out or [{"type": "text", "text": {"content": ""}}]


def _table_block(lines: list[str]) -> dict | None:
    """A run of | delimited lines -> one Notion table block."""
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                      # the |---|---| separator row
        rows.append(cells)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    return {
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {"type": "table_row",
                 "table_row": {"cells": [rich(c) for c in r + [""] * (width - len(r))]}}
                for r in rows
            ],
        },
    }


def to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    pending_table: list[str] = []

    def flush_table():
        if pending_table:
            block = _table_block(pending_table)
            if block:
                blocks.append(block)
            pending_table.clear()

    for raw in md.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            pending_table.append(stripped)
            continue
        flush_table()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": rich(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": rich(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": rich(stripped[2:])}})
        elif stripped.startswith(("- ", "* ")):
            blocks.append({"type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rich(stripped[2:])}})
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append({"type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": rich(
                               re.sub(r"^\d+\.\s", "", stripped))}})
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": rich(stripped)}})
    flush_table()
    return blocks


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
        nid = notice_id(name)

        row = notion.find_row(nid)
        if not row:
            print(f"  [{i}/{len(records)}] MISSING {nid}", file=sys.stderr)
            missing += 1
            continue

        page_id = row["id"]
        note = (props.get("Notes") or "").strip()
        body = (rec.get("body") or "").strip()
        blocks = to_blocks(body)
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
