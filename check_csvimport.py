"""Read-only spot-check: confirms the CSVIMPORT: batch actually landed in
Notion the way rescore_existing.py's log claimed — Fit/Status
distributions plus a few full rows, rather than trusting the log alone.

Usage:
    python check_csvimport.py
"""
from __future__ import annotations

from collections import Counter

from src.config import env, load_config
from src.notion_client import NotionClient


def _select(row: dict, name: str) -> str:
    return (((row.get("properties") or {}).get(name) or {}).get("select") or {}).get("name", "")


def _rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def _title(row: dict) -> str:
    return "".join(t.get("plain_text", "") for t in
                  ((row.get("properties") or {}).get("Name") or {}).get("title", []))


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({
        "property": cfg["notion"]["notice_id_property"],
        "rich_text": {"starts_with": "CSVIMPORT:"}})
    print(f"{len(rows)} CSVIMPORT: rows in the master database\n")

    fit_counts = Counter(_select(r, "Fit") for r in rows)
    status_counts = Counter(_select(r, "Status") for r in rows)
    ae_counts = Counter(_select(r, "AE") for r in rows)
    sdr_counts = Counter(_select(r, "SDR") for r in rows)
    partner_counts = Counter(_select(r, "Partner route") for r in rows)
    has_reason = sum(1 for r in rows if _rt(r, "Fit reason"))
    has_dims = sum(1 for r in rows if _rt(r, "Fit dimensions"))

    print("Fit distribution:", dict(fit_counts))
    print("Status distribution:", dict(status_counts))
    print("AE distribution:", dict(ae_counts))
    print("SDR distribution:", dict(sdr_counts))
    print("Partner route distribution:", dict(partner_counts))
    print(f"Fit reason populated: {has_reason}/{len(rows)}")
    print(f"Fit dimensions populated: {has_dims}/{len(rows)}")

    print("\nSample rows:")
    for r in rows[:5]:
        print(f"  {_title(r)[:60]:60s} Fit={_select(r,'Fit'):12s} "
              f"Status={_select(r,'Status'):16s} AE={_select(r,'AE'):8s} "
              f"SDR={_select(r,'SDR'):6s} Partner={_select(r,'Partner route')}")

    disqualified = [r for r in rows if _select(r, "Status") == "Disqualified"]
    print(f"\n{len(disqualified)} rows with Status=Disqualified, e.g.:")
    for r in disqualified[:5]:
        print(f"  {_title(r)[:60]} — {_rt(r, 'Fit reason')[:100]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
