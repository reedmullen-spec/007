"""Imports an already-researched CSV straight into the master database —
for exports shaped like the master schema itself (every column matches a
Notion property name: Project type, Work nature, AE, SDR, etc. already
filled in), as opposed to bulk_import.py's lightweight title/url/value
CSVs that still need qualify() to fill in the categorical fields.

Skips the Anthropic qualify() call entirely — those categorical fields
are already given, so there's nothing for a model to observe. Fit/Fit
profile/Fit reason/Fit dimensions/Product fit are NOT trusted from the
CSV, though: score_project() always runs (same rule as every other
manual-entry path — bulk_import.py, the intake form — always scores,
API call optional), so every imported row gets our own deterministic
Fit rather than whatever the source file's external process decided.
Concretely: a row where the categorical fields turn out to be missing or
wrong will misscore the same way a hand-entered row would, but at least
consistently, on the record, and by the same rule as everything else in
the database.

First use: converge_projects_qld_ca_or_wa.csv (290 rows, Queensland +
California/Oregon/Washington). Discovered on inspection and corrected
here rather than trusted as-is:
  - Value is in millions, not raw dollars (e.g. "3580" against a stated
    "250M+" band means $3,580M) — multiplied by 1e6 on the way in.
  - SDR was "reed" on every row including AE=lawson ones, but
    routing.ae_sdr_map says lawson -> alex — SDR is always re-derived
    from AE, never trusted from the CSV.
  - Partner route was a blanket "TBD", ignoring White Cap's US/Canada
    routing — re-derived via resolve_partner_route(), same rule
    ingest.py's main loop uses.
  - Verified was the literal string "__NO__" — parsed as a real checkbox.
  - Country was a full name ("United States") — normalized to ISO2 for
    consistency with rows ingested from TED/FTS/AusTender/SAM.
  - No row had a Notice ID — one is generated ({source-tag}:{slug}) so
    re-running the same CSV (or a corrected version) never duplicates.

Usage:
    python import_scored_csv.py data/converge_projects_qld_ca_or_wa.csv
    python import_scored_csv.py data/converge_projects_qld_ca_or_wa.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time

from src.config import env, load_config
from src.manual_entry import resolve_partner_route
from src.notion_client import NotionClient
from src.scoring import score_project

COUNTRY_NAME_TO_ISO = {
    "united states": "US", "united kingdom": "GB", "australia": "AU",
    "canada": "CA", "ireland": "IE", "italy": "IT", "belgium": "BE",
    "france": "FR", "germany": "DE", "spain": "ES", "netherlands": "NL",
}

MULTI_SELECT_SEP = ","


def _country_iso(name: str) -> str:
    name = (name or "").strip()
    return COUNTRY_NAME_TO_ISO.get(name.lower(), name)


def _bool(text: str) -> bool:
    return (text or "").strip().strip("_").lower() in ("yes", "true", "1")


def _number(text: str):
    text = (text or "").strip()
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _date_prop(text: str):
    text = (text or "").strip()
    return {"date": {"start": text[:10]}} if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _select_prop(text: str, valid: list[str] | None = None):
    text = (text or "").strip()
    if not text or (valid is not None and text not in valid):
        return None
    return {"select": {"name": text}}


def _rt_prop(notion: NotionClient, text: str):
    text = (text or "").strip()
    return notion._rt(text) if text else None


def build_properties(row: dict, cfg: dict, notion: NotionClient, notice_id: str) -> dict:
    N = NotionClient
    country_iso = _country_iso(row.get("Country", ""))
    region = (row.get("Region") or "").strip()
    ae = (row.get("AE") or "").strip()
    sdr = cfg["routing"].get("ae_sdr_map", {}).get(ae, "")
    partner_route = resolve_partner_route(country_iso, region, cfg)

    gc = (row.get("General contractor") or "").strip() or (row.get("General contractor/JV") or "").strip()
    value = _number(row.get("Value"))
    value_raw = value * 1_000_000 if value is not None else None
    use_case = [s.strip() for s in (row.get("Use case") or "").split(MULTI_SELECT_SEP)
               if s.strip() and s.strip() in NotionClient.USE_CASES]

    # Fit is always computed here, never trusted from the CSV — same rule
    # as bulk_import.py and the intake form: qualify() is skippable when
    # the categorical fields are already known, score_project() is not.
    scored = score_project({
        "title": row.get("Name", ""), "project_type": row.get("Project type", ""),
        "work_nature": row.get("Work nature", ""), "project_stage": row.get("Project stage", ""),
        "use_case": use_case, "concrete_opportunity": row.get("Concrete opportunity", ""),
        "expected_concrete_start": row.get("Expected concrete start", ""),
        "expected_completion": row.get("Expected completion", ""),
        "gc": gc, "value": value_raw, "ae": ae or "unassigned",
        "partner_route": partner_route, "summary": row.get("Summary", ""),
    }, cfg)
    dimensions_str = "; ".join(
        f"{k}: {'ok' if v['pass'] else 'off'} — {v['note']}"
        for k, v in scored["dimensions"].items())
    status = "Disqualified" if scored["fit"] == "Disqualified" \
        else ((row.get("Status") or "New").strip() or "New")

    props: dict = {
        cfg["notion"]["title_property"]: {
            "title": [{"text": {"content": (row.get("Name") or "")[:200]}}]},
        cfg["notion"]["notice_id_property"]: notion._rt(notice_id),
        "Status": {"select": {"name": status}},
        "Verified": {"checkbox": _bool(row.get("Verified", ""))},
        "AE": {"select": {"name": ae or "unassigned"}},
        "Partner route": {"select": {"name": partner_route}},
        "Fit": {"select": {"name": scored["fit"]}},
        "Fit reason": notion._rt(scored["reason"]),
        "Fit dimensions": notion._rt(dimensions_str),
    }
    if scored["profile"]:
        props["Fit profile"] = {"select": {"name": scored["profile"]}}
    if scored["products"]:
        props["Product fit"] = {"multi_select": [{"name": p} for p in scored["products"]]}
    if sdr:
        props["SDR"] = {"select": {"name": sdr}}
    if country_iso:
        props["Country"] = notion._rt(country_iso)
    if region:
        props["Region"] = {"select": {"name": region}}
    if value_raw is not None:
        props["Value"] = {"number": value_raw}
        band = ("Under 50M" if value_raw < 50_000_000
                else "50-250M" if value_raw < 250_000_000 else "250M+")
        props["Value band"] = {"select": {"name": band}}
    jv = (row.get("JV / parents") or "").strip()
    gc_jv = N._gc_jv_text(gc, jv)
    if gc_jv:
        props["General contractor/JV"] = notion._rt(gc_jv)

    text_fields = {
        "Location": row.get("Location", ""),
        "Client": row.get("Client", ""), "Concrete subcontractor": row.get("Concrete subcontractor", ""),
        "Competitor present": row.get("Competitor present", ""),
        "Expected concrete start": row.get("Expected concrete start", ""),
        "Summary": row.get("Summary", ""),
    }
    for name, text in text_fields.items():
        rt = _rt_prop(notion, text)
        if rt:
            props[name] = rt

    lat, lng = _number(row.get("Lat")), _number(row.get("Lng"))
    if lat is not None and lng is not None:
        props["Lat"] = {"number": lat}
        props["Lng"] = {"number": lng}

    if row.get("Notice URL", "").strip():
        props["Notice URL"] = {"url": row["Notice URL"].strip()}

    select_fields = {
        "Project type": (row.get("Project type", ""), N.PROJECT_TYPES),
        "Work nature": (row.get("Work nature", ""), N.WORK_NATURES),
        "Project stage": (row.get("Project stage", ""), N.PROJECT_STAGES),
        "Concrete opportunity": (row.get("Concrete opportunity", ""),
                                 ["Small", "Medium", "Large", "Unknown"]),
        "Source": (row.get("Source", "") or "MANUAL", None),
    }
    for name, (text, valid) in select_fields.items():
        sel = _select_prop(text, valid)
        if sel:
            props[name] = sel

    if use_case:
        props["Use case"] = {"multi_select": [{"name": u} for u in use_case]}

    completion = _date_prop(row.get("Expected completion", ""))
    if completion:
        props["Expected completion"] = completion

    return props


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--source-tag", default="CSVIMPORT",
                        help="notice_id prefix used for dedup (Source itself is "
                             "taken from each row's own Source column)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    if not args.dry_run:
        notion.ensure_schema()

    with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if "Name" not in (reader.fieldnames or []):
            print(f"ERROR: {args.csv_path}'s header row has no 'Name' column "
                  f"(found: {reader.fieldnames}) — this script expects a "
                  "master-schema-shaped export, not bulk_import.py's format.",
                  file=sys.stderr)
            return 1
        raw_rows = list(reader)
    print(f"{len(raw_rows)} rows in {args.csv_path}")

    existing_ids: set[str] = set()
    if not args.dry_run:
        db_id = notion.ensure_database()
        rows = notion.query_all_rows({
            "property": cfg["notion"]["notice_id_property"],
            "rich_text": {"starts_with": f"{args.source_tag}:"}}, database_id=db_id)
        for r in rows:
            vals = ((r.get("properties") or {}).get(cfg["notion"]["notice_id_property"])
                    or {}).get("rich_text", [])
            if vals:
                existing_ids.add(vals[0].get("plain_text", ""))

    created = skipped_dup = skipped_empty = failed = 0
    for i, row in enumerate(raw_rows, 1):
        name = (row.get("Name") or "").strip()
        if not name:
            skipped_empty += 1
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
        notice_id = f"{args.source_tag}:{slug}"
        if notice_id in existing_ids:
            skipped_dup += 1
            continue

        print(f"  [{i}/{len(raw_rows)}] {name[:80]}")
        if args.dry_run:
            created += 1
            continue

        try:
            props = build_properties(row, cfg, notion, notice_id)
            notion.create_page(notion.ensure_database(), props)
        except Exception as exc:
            print(f"    WARNING: import failed for {name[:60]}: {exc}", file=sys.stderr)
            failed += 1
            continue
        existing_ids.add(notice_id)
        created += 1
        time.sleep(0.4)

    verb = "Would create" if args.dry_run else "Created"
    print(f"{verb} {created} rows — {skipped_dup} dup, {skipped_empty} empty "
          f"name, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
