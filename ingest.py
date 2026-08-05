"""007 V1 ingest — build the project database silently. No Slack.

Pipeline per run:
  1. Fetch tenders (TED, FTS, AusTender, SAM) + news (trade press + watchlist)
  2. Filter and dedup (state + headline dedup + near-duplicate collapse)
  3. Cheap-qualify each candidate (structured observations, no fit — see
     src/scoring.py, which computes fit deterministically afterward)
  4. Geocode the best-known location (Nominatim, cached)
  5. Create one structured row per project in the Notion database, status=New

The Monday triage step (per-AE top-N) reads this database; humans and the
map/portal read it too. Capped per run so token spend stays bounded.

Usage:
    python ingest.py               # live
    python ingest.py --dry-run     # print what would be written
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict, deque

from src import state
from src.config import env, load_config
from src.filtering import filter_projects
from src.geocode import geocode
from src.models import Project
from src.notion_client import NotionClient
from src.qualify import qualify
from src.routing import resolve_ae
from src.scoring import score_project
from src.sources import austender, fts, sam, ted

from news import collect as collect_news, REGION_COUNTRY


def _round_robin(items: list[dict]) -> list[dict]:
    """Interleave candidates by source (TED/FTS/AUSTENDER/SAM/NEWS) so a
    single high-volume source (TED routinely dwarfs the others) can't
    monopolize the per-run cap. Preserves each source's internal order —
    only the interleaving across sources changes."""
    buckets: dict[str, deque] = defaultdict(deque)
    order: list[str] = []
    for item in items:
        src = item["source"]
        if src not in buckets:
            order.append(src)
        buckets[src].append(item)
    result = []
    while any(buckets[src] for src in order):
        for src in order:
            if buckets[src]:
                result.append(buckets[src].popleft())
    return result


def gather(cfg: dict, days_back: int, historical: bool = False) -> list[dict]:
    """All candidates (tenders + news) as uniform dicts."""
    seen = state.load("seen")
    out: list[dict] = []

    projects: list[Project] = []
    for name, source in (("TED", ted), ("FTS", fts),
                         ("AUSTENDER", austender), ("SAM", sam)):
        try:
            kwargs = {"days_back": days_back}
            if historical:
                if name == "TED":
                    kwargs.update(scope="ALL", max_pages=12)
                elif name == "FTS":
                    kwargs.update(max_pages=20)
                elif name == "AUSTENDER":
                    kwargs.update(max_pages=15)
            batch = source.fetch(cfg, **kwargs)
            print(f"{name}: {len(batch)} fetched")
            projects.extend(batch)
        except Exception as exc:
            print(f"WARNING: {name} fetch failed: {exc}", file=sys.stderr)

    for p in filter_projects(projects, cfg):
        if p.dedup_key in seen:
            continue
        region = ("au" if p.source == "AUSTENDER"
                  else getattr(p, "us_side", "us") if p.source == "SAM"
                  else "uk" if p.source == "FTS" else "eu")
        out.append({"dedup_key": p.dedup_key, "notice_id": p.notice_id,
                    "title": p.title, "source": p.source, "region": region,
                    "country": p.country, "buyer": p.buyer, "value": p.value,
                    "currency": p.currency, "url": p.url,
                    "deadline": p.deadline,
                    "us_state": getattr(p, "us_state", "")})

    news_fresh = [i for i in collect_news(cfg) if i.dedup_key not in seen]
    kept = []
    for cand in news_fresh:  # near-duplicate collapse (same story, reworded)
        if not any(len(cand.title_tokens & k.title_tokens)
                   / max(len(cand.title_tokens | k.title_tokens), 1) > 0.6
                   for k in kept):
            kept.append(cand)
    for i in kept:
        out.append({"dedup_key": i.dedup_key, "notice_id": i.dedup_key,
                    "title": i.title, "source": "NEWS", "region": i.region,
                    "country": REGION_COUNTRY.get(i.region.split("_")[0], ""),
                    "buyer": i.entity, "value": None, "currency": "",
                    "url": i.url})
    return _round_robin(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days-back", type=int, default=2)
    parser.add_argument("--max-rows", type=int, default=0,
                        help="Override the per-run row cap (backfill)")
    parser.add_argument("--historical", action="store_true",
                        help="Backfill mode: include expired TED notices, "
                             "paginate deeper on all sources")
    args = parser.parse_args()

    cfg = load_config()
    candidates = gather(cfg, args.days_back, historical=args.historical)
    cap = args.max_rows or cfg["ingest"].get("max_rows_per_run", 60)
    print(f"{len(candidates)} new candidates"
          + (f" (capped to {cap})" if len(candidates) > cap else ""))
    candidates = candidates[:cap]

    if args.dry_run:
        for c in candidates:
            print(f"  [{c['source']}/{c['region']}] {c['title'][:90]}")
        return 0

    api_key = env("ANTHROPIC_API_KEY")
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    notion.cfg["_project_types"] = cfg["ingest"]["project_types"]
    notion.ensure_schema()

    seen = state.load("seen")
    import datetime as _dt
    today = _dt.date.today().isoformat()
    written = 0
    for c in candidates:
        # Notion is the dedup authority: never add a project that's already
        # a row, even if local state was lost.
        try:
            if notion.find_row(c["notice_id"]):
                seen[c["dedup_key"]] = {"ingested": True, "dedup": "notion"}
                continue
        except Exception as exc:
            print(f"WARNING: Notion dedup check failed for {c['notice_id']}: {exc}",
                  file=sys.stderr)
        try:
            q = qualify(api_key, cfg, title=c["title"], source=c["source"],
                        country=c["country"], buyer=c["buyer"],
                        value=(f"{c['value']:,.0f} {c['currency']}"
                               if c["value"] else ""), url=c["url"])
        except Exception as exc:
            print(f"WARNING: qualify failed for {c['title'][:60]}: {exc}",
                  file=sys.stderr)
            q = {"summary": "", "general_contractor": c["buyer"], "client": "",
                 "jv_parents": "", "project_type": "Other",
                 "work_nature": "Unknown", "project_stage": "Unknown",
                 "use_case": ["Unknown"], "product_fit": [],
                 "concrete_opportunity": "Unknown",
                 "expected_concrete_start": "Unknown", "location": "",
                 "competitor": ""}

        lat = lng = None
        if cfg["ingest"].get("geocode", True):
            coords = geocode(q.get("location", ""), c["country"])
            if coords:
                lat, lng = coords

        ae = resolve_ae(q.get("general_contractor") or c["buyer"],
                        c["country"], cfg, None) or "unassigned"
        if c["source"] == "SAM" and c.get("us_state"):
            from src.routing import us_state_ae
            ae = us_state_ae(c["us_state"], cfg) or ae
        partner = ("Hakron" if c["country"].upper() in
                   [x.upper() for x in cfg.get("hakron_skip_contacts_countries", [])]
                   else "White Cap" if c["region"].startswith(("us", "ca"))
                   else "TBD")

        # Score AFTER ae/partner are resolved — the coverage/access
        # dimensions need the real values, not "unassigned"/"TBD" placeholders.
        scored = score_project({**q, "title": c["title"],
                                "gc": q.get("general_contractor", ""),
                                "value": c["value"], "ae": ae,
                                "partner_route": partner}, cfg)
        dimensions_str = "; ".join(
            f"{k}: {'ok' if v['pass'] else 'off'} — {v['note']}"
            for k, v in scored["dimensions"].items())

        try:
            notion.create_project_row({
                "title": c["title"], "notice_id": c["notice_id"],
                "source": c["source"], "region": c["region"],
                "country": c["country"], "location": q.get("location", ""),
                "gc": q.get("general_contractor", ""),
                "project_type": q.get("project_type", "Other"),
                "stage": q.get("project_stage", "Unknown"),
                "work_nature": q.get("work_nature", "Unknown"),
                "concrete_opportunity": q.get("concrete_opportunity", "Unknown"),
                "competitor": q.get("competitor", ""),
                "value": c["value"], "currency": c["currency"] or "EUR",
                "concrete_start": q.get("expected_concrete_start", ""),
                "fit": scored["fit"],
                "fit_profile": scored["profile"],
                "fit_reason": scored["reason"],
                "fit_dimensions": dimensions_str,
                "summary": q.get("summary", ""),
                "url": c["url"], "lat": lat, "lng": lng,
                "client": q.get("client", "") or c["buyer"],
                "jv_parents": q.get("jv_parents", ""),
                "use_case": q.get("use_case", []),
                "product_fit": scored["products"] or q.get("product_fit", []),
                "ae": ae, "partner_route": partner,
                "deadline": c.get("deadline", ""), "announced": today,
            })
            seen[c["dedup_key"]] = {"ingested": True}
            written += 1
            time.sleep(0.4)   # Notion rate courtesy
        except Exception as exc:
            print(f"WARNING: Notion write failed for {c['title'][:60]}: {exc}",
                  file=sys.stderr)

    state.save("seen", seen)
    print(f"Wrote {written} project rows to Notion")

    # Sweep: qualify rows humans added by hand (no Notice ID yet).
    swept = sweep_manual_rows(cfg, api_key, notion)
    if swept:
        print(f"Qualified {swept} manually-added rows")
    return 0


def sweep_manual_rows(cfg: dict, api_key: str, notion: NotionClient) -> int:
    """Rows added by hand (White Cap intel, AE tips) have no Notice ID.
    Qualify them, geocode, stamp Source=MANUAL, so triage can prioritise."""
    import re as _re
    rows = notion.query_rows({"property": cfg["notion"]["notice_id_property"],
                              "rich_text": {"is_empty": True}})
    count = 0
    for row in rows:
        title = notion.row_title(row)
        if not title:
            continue
        try:
            q = qualify(api_key, cfg, title=title, source="MANUAL",
                        country="", buyer="")
        except Exception as exc:
            print(f"WARNING: manual qualify failed for {title[:50]}: {exc}",
                  file=sys.stderr)
            continue
        slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        # Manually-added rows have no resolved AE/partner/value at sweep
        # time — score honestly against what's known; coverage/access will
        # legitimately read as "off" until an AE or value is set by hand.
        existing_value = (row.get("properties") or {}).get("Value", {}).get("number")
        scored = score_project({**q, "title": title,
                                "gc": q.get("general_contractor", ""),
                                "value": existing_value, "ae": "unassigned",
                                "partner_route": "TBD"}, cfg)
        dimensions_str = "; ".join(
            f"{k}: {'ok' if v['pass'] else 'off'} — {v['note']}"
            for k, v in scored["dimensions"].items())
        props = {
            cfg["notion"]["notice_id_property"]: NotionClient._rt(f"MANUAL:{slug}"),
            "Source": {"select": {"name": "MANUAL"}},
            "Summary": NotionClient._rt(q.get("summary", "")),
            "General contractor": NotionClient._rt(q.get("general_contractor", "")),
            "Project stage": {"select": {"name": q.get("project_stage", "Unknown")}},
            "Work nature": {"select": {"name": q.get("work_nature", "Unknown")}},
            "Expected concrete start": NotionClient._rt(q.get("expected_concrete_start", "")),
            "Fit": {"select": {"name": scored["fit"]}},
            "Fit reason": NotionClient._rt(scored["reason"]),
            "Fit dimensions": NotionClient._rt(dimensions_str),
            "Location": NotionClient._rt(q.get("location", "")),
        }
        if scored["profile"]:
            props["Fit profile"] = {"select": {"name": scored["profile"]}}
        if q.get("project_type"):
            props["Project type"] = {"select": {"name": q["project_type"]}}
        coords = geocode(q.get("location", "")) if cfg["ingest"].get("geocode") else None
        if coords:
            props["Lat"] = {"number": coords[0]}
            props["Lng"] = {"number": coords[1]}
        notion.update_properties(row["id"], props)
        count += 1
        time.sleep(0.4)
    return count


if __name__ == "__main__":
    raise SystemExit(main())
