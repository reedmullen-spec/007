"""Bulk-imports a hand-researched list of projects (CSV) through the same
qualify + score + route pipeline as ingest.py — for lists gathered outside
the automated tender/news feeds. First use: Jeremy's Queensland set,
~AUD 40M floor.

CSV columns (header row required, all optional except at least one of
title/url): title, url, country, buyer, value, currency, region.
- If title is blank but url is present, the title is derived from the
  fetched page.
- region is derived from country (GB->uk, US->us, CA->ca, AU->au, else eu)
  if left blank.
- value is compared to --min-value as-is, in whatever currency the row's
  `currency` column says — no FX conversion.

Rows are stamped with notice_id "{source-tag}:{slug}" so re-running the
same CSV (or a corrected version of it) never creates duplicates.

Usage:
    python bulk_import.py queensland.csv --min-value 40000000
    python bulk_import.py queensland.csv --min-value 40000000 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time

from src.config import env, load_config
from src.manual_entry import compute_fields, region_for_country
from src.notion_client import NotionClient


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--min-value", type=float, default=None,
                        help="Skip rows below this value, in the row's own currency")
    parser.add_argument("--source-tag", default="BULK",
                        help="Notion Source value stamped on every imported row, "
                             "and the notice_id prefix used for dedup")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    api_key = env("ANTHROPIC_API_KEY")
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    if not args.dry_run:
        notion.ensure_schema()

    existing_ids: set[str] = set()
    if not args.dry_run:
        rows = notion.query_all_rows({
            "property": cfg["notion"]["notice_id_property"],
            "rich_text": {"starts_with": f"{args.source_tag}:"}})
        existing_ids = {_prop_rt(r, cfg["notion"]["notice_id_property"]) for r in rows}

    with open(args.csv_path, newline="", encoding="utf-8") as fh:
        raw_rows = list(csv.DictReader(fh))
    print(f"{len(raw_rows)} rows in {args.csv_path}")

    created = skipped_dup = skipped_value = skipped_empty = failed = 0
    for i, raw in enumerate(raw_rows, 1):
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        country = (raw.get("country") or "").strip()
        buyer = (raw.get("buyer") or "").strip()
        currency = (raw.get("currency") or "").strip() or "USD"
        region = (raw.get("region") or "").strip() or region_for_country(country)
        try:
            value = float(raw["value"]) if raw.get("value") else None
        except ValueError:
            value = None

        if not title and not url:
            skipped_empty += 1
            continue
        if args.min_value is not None and value is not None and value < args.min_value:
            skipped_value += 1
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", (title or url).lower()).strip("-")[:60]
        notice_id = f"{args.source_tag}:{slug}"
        if notice_id in existing_ids:
            skipped_dup += 1
            continue

        label = (title or url)[:80]
        print(f"  [{i}/{len(raw_rows)}] {label}")
        if args.dry_run:
            created += 1
            continue

        try:
            fields = compute_fields(cfg, api_key, title=title, source=args.source_tag,
                                    country=country, buyer=buyer, value=value,
                                    currency=currency, url=url, region=region)
        except Exception as exc:
            print(f"    WARNING: qualify failed for {label}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if not fields:
            skipped_empty += 1
            continue

        notion.create_project_row({
            **fields, "notice_id": notice_id, "source": args.source_tag,
            "region": region, "country": country, "url": url, "deadline": "",
        })
        existing_ids.add(notice_id)
        created += 1
        time.sleep(0.4)

    verb = "Would create" if args.dry_run else "Created"
    print(f"{verb} {created} rows — {skipped_dup} dup, {skipped_value} below "
          f"value floor, {skipped_empty} empty title/url, {failed} qualify failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
