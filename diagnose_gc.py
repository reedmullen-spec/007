"""One-off diagnostic: General contractor/JV capture rate by source, to
confirm whether a missing contractor is expected (pre-award notices
genuinely don't have one) or something worth investigating further.

Usage:
    python diagnose_gc.py
"""
from __future__ import annotations

from collections import defaultdict

from src.config import env, load_config
from src.notion_client import NotionClient


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({})
    print(f"Scanned {len(rows)} total rows\n")

    totals = defaultdict(int)
    has_gc = defaultdict(int)
    for row in rows:
        source = _prop_select(row, "Source") or "(none)"
        totals[source] += 1
        if _prop_rt(row, "General contractor/JV").strip():
            has_gc[source] += 1

    print(f"{'Source':<12} {'Total':>6} {'Has GC/JV':>10} {'%':>6}")
    for source in sorted(totals, key=lambda s: -totals[s]):
        t = totals[source]
        g = has_gc[source]
        print(f"{source:<12} {t:>6} {g:>10} {g/t*100:>5.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
