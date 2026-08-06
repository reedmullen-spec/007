"""Sweeps the standalone "007 — Submit a project" intake database into
the master. The intake database is deliberately separate from
"007 Projects" — its own page, its own Notion sharing, a minimal
purpose-built schema — so anyone can be given access to submit a project
without ever touching the master, which stays restricted to Reed and
Issam. Runs inside ingest.py's regular schedule, same as sweep_manual_rows
(bare rows added directly on the master).
"""
from __future__ import annotations

import re
import sys
import time

from .manual_entry import compute_fields, region_for_country
from .notion_client import NotionClient


def _prop_text(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def sweep_intake_rows(cfg: dict, api_key: str, notion: NotionClient) -> int:
    intake_db = notion.ensure_intake_database()
    rows = notion.query_all_rows(
        {"property": "Imported", "checkbox": {"equals": False}},
        database_id=intake_db)
    if not rows:
        return 0

    # A project submitted twice (two different intake rows) shouldn't
    # become two master rows — same INTAKE:{slug} dedup key as bulk_import.
    existing_ids = {
        _prop_text(r, cfg["notion"]["notice_id_property"])
        for r in notion.query_all_rows({
            "property": cfg["notion"]["notice_id_property"],
            "rich_text": {"starts_with": "INTAKE:"}})}

    count = 0
    for row in rows:
        props = row.get("properties") or {}
        title = "".join(t.get("plain_text", "")
                        for t in (props.get("Title") or {}).get("title", []))
        url = (props.get("Notice URL") or {}).get("url") or ""
        country = _prop_text(row, "Country")
        value = (props.get("Value") or {}).get("number")
        currency = ((props.get("Currency") or {}).get("select") or {}).get("name") or "USD"
        notes = _prop_text(row, "Notes")

        if not title and not url:
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", (title or url).lower()).strip("-")[:60]
        notice_id = f"INTAKE:{slug}"
        if notice_id in existing_ids:
            # Already imported under another intake row — stop resurfacing it.
            notion.update_properties(row["id"], {"Imported": {"checkbox": True}})
            continue

        region = region_for_country(country)
        try:
            fields = compute_fields(cfg, api_key, title=title, source="INTAKE",
                                    country=country, value=value, currency=currency,
                                    url=url, region=region, notes=notes)
        except Exception as exc:
            print(f"WARNING: intake qualify failed for {(title or url)[:50]}: {exc}",
                  file=sys.stderr)
            continue  # retry on the next sweep
        if not fields:
            continue

        notion.create_project_row({
            **fields, "notice_id": notice_id, "source": "INTAKE",
            "region": region, "country": country, "url": url, "deadline": "",
        })
        existing_ids.add(notice_id)
        notion.update_properties(row["id"], {"Imported": {"checkbox": True}})
        count += 1
        time.sleep(0.4)
    return count
