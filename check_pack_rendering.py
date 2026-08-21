"""Verifies notion_client.markdown_to_blocks() against the live Notion API.

The converter is a pure function and easy to check offline, but "Notion
accepts this block" is not something offline validation can establish, and
enrich.py's append_pack() call is not wrapped — a block shape the API
rejects breaks enrichment at pack-write time, in the approvals flow, on a
real deal. So this sends one pack exercising every shape the converter can
emit to a scratch page, reads it back, and archives the page again.

    python check_pack_rendering.py
    python check_pack_rendering.py --keep     # leave the page for eyeballing

Exits non-zero if the API rejects the write or a shape does not survive the
round trip.
"""
from __future__ import annotations

import argparse
import sys

import requests

from src.config import env, load_config
from src.notion_client import BASE, VERSION, NotionClient, markdown_to_blocks

# Every branch of the converter, including the four shapes that reach the API
# for the first time with tables/bold: code spans, links, quotes, dividers.
SAMPLE = """# Pack rendering check

## TL;DR

- **Bold** survives
- `code` survives
- A [link](https://example.com/pack) survives
- A NABERS 5-5.5\\* rating keeps its asterisk and loses its backslash

## Decomposition

| Package | Value | Relevance |
|---|---:|---|
| Substructure | £40m | **High** |
| Frame | £120m | Repeating floorplates |

---

> A quote block.

1. First numbered item
2) Second, written with a paren

### Third-level heading

An ordinary paragraph.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="don't archive the scratch page afterwards")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    blocks = markdown_to_blocks(SAMPLE)
    print(f"{len(blocks)} blocks from the sample pack")

    page = notion._check(notion.session.post(
        f"{BASE}/pages",
        json={"parent": {"page_id": cfg["notion"]["parent_page_id"]},
              "properties": {"title": [{"text": {"content":
                             "007 — pack rendering check (safe to delete)"}}]}},
        timeout=30))
    page_id = page["id"]
    print(f"scratch page {page_id}")

    failed = False
    try:
        notion.append_pack(page_id, SAMPLE)
        print("append_pack: accepted by the API")

        got = notion._check(notion.session.get(
            f"{BASE}/blocks/{page_id}/children",
            params={"page_size": 100}, timeout=30))["results"]
        kinds = [b["type"] for b in got]
        print(f"read back {len(got)} blocks: {kinds}")

        expected = {"heading_1", "heading_2", "heading_3", "bulleted_list_item",
                    "numbered_list_item", "table", "divider", "quote", "paragraph"}
        missing = expected - set(kinds)
        if missing:
            print(f"FAIL: shapes did not survive the round trip: {sorted(missing)}",
                  file=sys.stderr)
            failed = True

        # the marks, on the bullets
        marks = {"bold": False, "code": False, "link": False, "escaped_star": False}
        for b in got:
            if b["type"] != "bulleted_list_item":
                continue
            for seg in b["bulleted_list_item"]["rich_text"]:
                if seg.get("annotations", {}).get("bold"):
                    marks["bold"] = True
                if seg.get("annotations", {}).get("code"):
                    marks["code"] = True
                if (seg.get("text") or {}).get("link"):
                    marks["link"] = True
                if "5-5.5*" in seg.get("plain_text", ""):
                    marks["escaped_star"] = True
        print(f"inline marks round-tripped: {marks}")
        if not all(marks.values()):
            print(f"FAIL: lost {[k for k, v in marks.items() if not v]}", file=sys.stderr)
            failed = True

        table = next((b for b in got if b["type"] == "table"), None)
        if table:
            rows = notion._check(notion.session.get(
                f"{BASE}/blocks/{table['id']}/children", timeout=30))["results"]
            print(f"table: {table['table']['table_width']} cols, {len(rows)} rows, "
                  f"header={table['table']['has_column_header']}")
            if len(rows) != 3:
                print(f"FAIL: expected 3 table rows, got {len(rows)}", file=sys.stderr)
                failed = True
    except requests.HTTPError as exc:
        print(f"FAIL: the API rejected the write: {exc}", file=sys.stderr)
        failed = True
    finally:
        if args.keep:
            print(f"kept: https://notion.so/{page_id.replace('-', '')}")
        else:
            notion.session.patch(f"{BASE}/pages/{page_id}",
                                 json={"archived": True}, timeout=30)
            print("scratch page archived")

    print("FAILED" if failed else "OK — every shape the converter emits is accepted")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
