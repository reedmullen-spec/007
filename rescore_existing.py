"""Retroactive re-scoring — re-runs src/scoring.py's deterministic scorer
against existing "New" rows that were scored before scoring.py existed
(qualify-decided fit, no profile/dimensions, 3-band lowercase). Reads only
what's already stored on each row — no fresh qualify() call, no API cost
beyond Notion reads/writes, since scoring is designed to be reproducible
from stored observations alone (see src/scoring.py's docstring).

Rows that now score Disqualified also get Status flipped to Disqualified
(never deleted — see CLAUDE.md), matching how cleanup_filters.py treats
disqualification, so the master's Status field stays the reliable signal
of what's actually still active.

Also doubles as the way to run scoring on a CSV import: import_scored_csv.py
deliberately skips score_project() (it trusts the source file's own Fit
judgment), so --notice-id-prefix targets just that batch instead of the
default full "New" sweep.

Usage:
    python rescore_existing.py               # live, all "New" rows
    python rescore_existing.py --dry-run     # print what would change
    python rescore_existing.py --notice-id-prefix CSVIMPORT: --dry-run
"""
from __future__ import annotations

import argparse
import time

from src.config import env, load_config
from src.notion_client import NotionClient
from src.scoring import score_project


def _prop_select(row: dict, name: str) -> str:
    return (((row.get("properties") or {}).get(name) or {}).get("select") or {}).get("name", "")


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def _prop_number(row: dict, name: str):
    return ((row.get("properties") or {}).get(name) or {}).get("number")


def _prop_ms(row: dict, name: str) -> list[str]:
    return [o.get("name", "") for o in
            ((row.get("properties") or {}).get(name) or {}).get("multi_select", [])]


def _prop_title(row: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in
                  ((row.get("properties") or {}).get(name) or {}).get("title", []))


def _prop_date(row: dict, name: str) -> str:
    return (((row.get("properties") or {}).get(name) or {}).get("date") or {}).get("start") or ""


def row_to_fields(row: dict) -> dict:
    """Reconstruct score_project()'s expected input from what's already
    stored on the row — the same data ingest.py wrote it from originally.
    "Expected completion" is a date property (ISO string); scoring.
    resolve_date() round-trips that format as well as qualify's original
    fuzzy text, so no reformatting is needed here."""
    return {
        "title": _prop_title(row, "Name"),
        "project_type": _prop_select(row, "Project type"),
        "work_nature": _prop_select(row, "Work nature"),
        "project_stage": _prop_select(row, "Project stage"),
        "use_case": _prop_ms(row, "Use case"),
        "concrete_opportunity": _prop_select(row, "Concrete opportunity"),
        "expected_concrete_start": _prop_rt(row, "Expected concrete start"),
        "expected_completion": _prop_date(row, "Expected completion"),
        "gc": _prop_rt(row, "General contractor/JV"),
        "value": _prop_number(row, "Value"),
        "ae": _prop_select(row, "AE") or "unassigned",
        "partner_route": _prop_select(row, "Partner route") or "TBD",
        "summary": _prop_rt(row, "Summary"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notice-id-prefix", default="",
                        help="Rescore rows whose notice_id starts with this "
                             "(e.g. CSVIMPORT:) instead of the default sweep "
                             "of every Status=New row — use this to run "
                             "scoring on one import batch regardless of the "
                             "Status values it was imported with.")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    if args.notice_id_prefix:
        rows = notion.query_all_rows({
            "property": cfg["notion"]["notice_id_property"],
            "rich_text": {"starts_with": args.notice_id_prefix}})
        print(f"Rescoring {len(rows)} rows with notice_id starting "
              f"'{args.notice_id_prefix}'")
    else:
        rows = notion.query_all_rows({"property": "Status", "select": {"equals": "New"}})
        print(f"Rescoring {len(rows)} 'New' rows")

    changed = disqualified = 0
    for row in rows:
        fields = row_to_fields(row)
        scored = score_project(fields, cfg)
        old_fit = _prop_select(row, "Fit")
        title = fields["title"] or "(untitled)"

        if scored["fit"] != old_fit:
            changed += 1
            print(f"  {title[:70]}: {old_fit or '(none)'} -> {scored['fit']} "
                  f"— {scored['reason']}")
        if scored["fit"] == "Disqualified":
            disqualified += 1

        if args.dry_run:
            continue

        dimensions_str = "; ".join(
            f"{k}: {'ok' if v['pass'] else 'off'} — {v['note']}"
            for k, v in scored["dimensions"].items())
        props = {
            "Fit": {"select": {"name": scored["fit"]}},
            "Fit reason": NotionClient._rt(scored["reason"]),
            "Fit dimensions": NotionClient._rt(dimensions_str),
            "Fit profile": ({"select": {"name": scored["profile"]}}
                            if scored["profile"] else {"select": None}),
        }
        if scored["products"]:
            props["Product fit"] = {"multi_select": [
                {"name": p} for p in scored["products"]]}
        if scored["fit"] == "Disqualified":
            props["Status"] = {"select": {"name": "Disqualified"}}
        notion.update_properties(row["id"], props)
        time.sleep(0.4)

    verb = "Would change" if args.dry_run else "Changed"
    print(f"{verb} {changed} of {len(rows)} rows' Fit band "
          f"({disqualified} now Disqualified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
