"""Re-check TED/FTS notices for a contractor once a project's tender
deadline has passed. ted.py/fts.py's winner-name / awards[].suppliers
capture (see their docstrings) only helps NEW ingests that happen to land
directly on an award-type notice — this backfills EXISTING pre-award rows
once an award has since been published for them.

TED: exact buyer-name match (a buyer's registered name doesn't change
between their tender and its later award) scoped to notice-type=
can-standard, then title-token similarity to pick the right award among
that buyer's other unrelated awards. This is a fuzzy match — flagged in
Fit reason, "Verified" left False for a human to confirm.

--similarity-threshold defaults to 0.4, NOT the 0.6 news.py uses for its
near-duplicate collapse — tested against real TED data: a realistically
reworded near-duplicate of an actual award title scored 0.5 against its
true match while staying under 0.27 against four unrelated awards from
the same buyer. 0.6 would have missed the true match; 0.4 leaves margin
without (in this one test) crossing into the unrelated titles' scores.
This is a best-effort heuristic, not a precise mechanism — treat the
threshold as tunable and worth revisiting once real dry-run output can be
checked against known-correct matches.

FTS: exact OCID match. An award-stage OCDS release shares its OCID with
the original tender release for the same procurement (verified live,
Aug 2026) — not a guess, so "Verified" is set True.

CANADA: exact referenceNumber/solicitationNumber match against the
CanadaBuys contract history CSV (same field names as the tender-notices
file it's matched against — verified live, Aug 2026) — also an exact
match, "Verified" set True. Also backfills Value, since CanadaBuys
withholds estimated value pre-award but discloses the actual awarded
value once a contract exists — except when that value is 0, which (also
verified live) means undisclosed rather than a genuine free contract, so
it's left blank rather than written as a real value.

Known limitation: TED matching uses the row's stored "Client" field as a
buyer-name proxy (qualify.py's model-chosen client, falling back to the
raw buyer name) — if the model reworded the buyer's name rather than using
it verbatim, this misses that row. A missed match, not a wrong one.

Usage:
    python recheck_awards.py                                # live
    python recheck_awards.py --dry-run                       # print matches only
    python recheck_awards.py --lookback-days 365              # override the award-search window
    python recheck_awards.py --similarity-threshold 0.5        # stricter fuzzy matching
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import time
from collections import defaultdict

import requests

from src.config import env, load_config
from src.notion_client import NotionClient
from src.sources.ted import _all_strings, _first_text

ACTIVE_STATUSES = ["New", "This week", "Active Contact", "Recontact later"]
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
FTS_SEARCH_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CANADA_CONTRACT_HISTORY_URL = ("https://canadabuys.canada.ca/opendata/pub/"
                               "contractHistoryComplete-contratsOctroyesComplet.csv")


def _title_tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "to", "of", "in", "on", "for", "and",
            "with", "at", "its", "as"}
    norm = re.sub(r"[^a-z0-9 ]+", "", title.lower())
    return {t for t in norm.split() if t not in stop and len(t) > 2}


def _similar(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def _prop_title(row: dict, name: str = "Name") -> str:
    return "".join(t.get("plain_text", "") for t in
                  ((row.get("properties") or {}).get(name) or {}).get("title", []))


def _pending_rows(notion: NotionClient, source: str) -> list[dict]:
    today = dt.date.today().isoformat()
    rows = notion.query_all_rows({"and": [
        {"property": "Source", "select": {"equals": source}},
        {"or": [{"property": "Status", "select": {"equals": s}} for s in ACTIVE_STATUSES]},
        {"property": "Tender deadline", "date": {"before": today}},
    ]})
    return [r for r in rows if not _prop_rt(r, "General contractor/JV").strip()]


def _ted_awards_for_buyer(session: requests.Session, buyer: str, since: str) -> list[dict]:
    query = (f'(buyer-name="{buyer}") AND (notice-type=can-standard) '
             f'AND (publication-date>={since})')
    body = {"query": query,
            "fields": ["publication-number", "notice-title", "winner-name",
                      "organisation-name-tenderer"],
            "limit": 100, "scope": "ACTIVE", "paginationMode": "ITERATION",
            "checkQuerySyntax": False}
    resp = session.post(TED_SEARCH_URL, json=body, timeout=60)
    if resp.status_code != 200:
        return []
    return resp.json().get("notices", [])


def recheck_ted(notion: NotionClient, args) -> None:
    rows = _pending_rows(notion, "TED")
    print(f"TED: {len(rows)} pending rows (no GC, deadline passed)")
    by_client: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        client = _prop_rt(row, "Client").strip()
        if client:
            by_client[client].append(row)
    since = (dt.date.today() - dt.timedelta(days=args.lookback_days)).strftime("%Y%m%d")

    session = requests.Session()
    matched = 0
    for client, client_rows in by_client.items():
        try:
            awards = _ted_awards_for_buyer(session, client, since)
        except Exception as exc:
            print(f"  WARNING: TED lookup failed for {client!r}: {exc}")
            continue
        for row in client_rows:
            title = _prop_title(row)
            best = None
            for award in awards:
                sim = _similar(title, _first_text(award.get("notice-title")))
                if sim > args.similarity_threshold and (best is None or sim > best[1]):
                    best = (award, sim)
            if not best:
                continue
            award, sim = best
            winner = _first_text(award.get("winner-name"))
            tenderers = list(dict.fromkeys(_all_strings(award.get("organisation-name-tenderer"))))
            gc = winner or (tenderers[0] if tenderers else "")
            if not gc:
                continue
            jv = ", ".join(tenderers) if len(tenderers) > 1 else ""
            matched += 1
            print(f"  [{sim:.2f}] {title[:60]!r} -> {gc!r}" + (f" (jv={jv!r})" if jv else ""))
            if not args.dry_run:
                existing = _prop_rt(row, "Fit reason")
                note = (f"GC backfilled from award notice "
                        f"{award.get('publication-number')} "
                        f"(fuzzy title match {sim:.2f} — verify)")
                notion.update_properties(row["id"], {
                    "General contractor/JV": NotionClient._rt(NotionClient._gc_jv_text(gc, jv)),
                    "Fit reason": NotionClient._rt(f"{existing} · {note}" if existing else note),
                })
                time.sleep(0.4)
    print(f"TED: backfilled {matched} rows")


def recheck_fts(notion: NotionClient, cfg: dict, args) -> None:
    rows = _pending_rows(notion, "FTS")
    print(f"FTS: {len(rows)} pending rows (no GC, deadline passed)")
    if not rows:
        return

    session = requests.Session()
    now = dt.datetime.now(dt.timezone.utc)
    params = {"updatedFrom": (now - dt.timedelta(days=args.lookback_days)).strftime("%Y-%m-%dT%H:%M:%S"),
              "updatedTo": now.strftime("%Y-%m-%dT%H:%M:%S"),
              "stages": ["award"], "limit": 100}
    awards_by_ocid: dict[str, dict] = {}
    url, first = FTS_SEARCH_URL, True
    for _ in range(20):
        if not url:
            break
        resp = session.get(url, params=params if first else None, timeout=60)
        first = False
        if resp.status_code != 200:
            break
        pkg = resp.json()
        for rel in pkg.get("releases", []):
            ocid = rel.get("ocid")
            if ocid:
                awards_by_ocid[str(ocid)] = rel
        url = (pkg.get("links") or {}).get("next")

    matched = 0
    for row in rows:
        ocid = _prop_rt(row, cfg["notion"]["notice_id_property"]).strip()
        release = awards_by_ocid.get(ocid)
        if not release:
            continue
        suppliers: list[str] = []
        for award in release.get("awards", []) or []:
            for s in award.get("suppliers", []) or []:
                name = s.get("name", "")
                if name and name not in suppliers:
                    suppliers.append(name)
        if not suppliers:
            continue
        gc = suppliers[0]
        jv = ", ".join(suppliers) if len(suppliers) > 1 else ""
        matched += 1
        print(f"  {_prop_title(row)[:60]!r} -> {gc!r}" + (f" (jv={jv!r})" if jv else ""))
        if not args.dry_run:
            notion.update_properties(row["id"], {
                "General contractor/JV": NotionClient._rt(NotionClient._gc_jv_text(gc, jv)),
                "Verified": {"checkbox": True},
            })
            time.sleep(0.4)
    print(f"FTS: backfilled {matched} rows")


def recheck_canada(notion: NotionClient, cfg: dict, args) -> None:
    rows = _pending_rows(notion, "CANADA")
    print(f"CANADA: {len(rows)} pending rows (no GC, deadline passed)")
    if not rows:
        return

    session = requests.Session()
    resp = session.get(CANADA_CONTRACT_HISTORY_URL, timeout=60,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; 007RadarBot/1.0)"})
    if resp.status_code != 200:
        print(f"  WARNING: CanadaBuys contract history fetch failed ({resp.status_code})")
        return

    awards_by_ref: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    for r in reader:
        ref = (r.get("referenceNumber-numeroReference") or "").strip()
        sol = (r.get("solicitationNumber-numeroSollicitation") or "").strip()
        if ref:
            awards_by_ref.setdefault(ref, r)
        if sol:
            awards_by_ref.setdefault(sol, r)

    matched = 0
    for row in rows:
        notice_id = _prop_rt(row, cfg["notion"]["notice_id_property"]).strip()
        award = awards_by_ref.get(notice_id)
        if not award:
            continue
        gc = (award.get("supplierLegalName-nomLegalFournisseur-eng")
              or award.get("supplierOperatingName-nomCommercialFournisseur-eng")
              or award.get("supplierStandardizedName-nomNormaliseFournisseur-eng")
              or "").strip()
        if not gc:
            continue

        value_raw = (award.get("totalContractValue-valeurTotaleContrat")
                     or award.get("contractAmount-montantContrat") or "").strip()
        try:
            value = float(value_raw) if value_raw else None
        except ValueError:
            value = None
        if value == 0:
            value = None   # 0 means undisclosed here, not a genuine free contract

        matched += 1
        print(f"  {_prop_title(row)[:60]!r} -> {gc!r}"
              + (f" (value={value:,.0f} CAD)" if value else ""))
        if not args.dry_run:
            props = {"General contractor/JV": NotionClient._rt(gc),
                     "Verified": {"checkbox": True}}
            if value is not None:
                band = ("Under 50M" if value < 50_000_000
                        else "50-250M" if value < 250_000_000 else "250M+")
                props["Value"] = {"number": value}
                props["Value band"] = {"select": {"name": band}}
            notion.update_properties(row["id"], props)
            time.sleep(0.4)
    print(f"CANADA: backfilled {matched} rows")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=730,
                        help="How far back to search for award notices")
    parser.add_argument("--similarity-threshold", type=float, default=0.4,
                        help="Minimum title-token Jaccard similarity for a TED fuzzy match")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    recheck_ted(notion, args)
    recheck_fts(notion, cfg, args)
    recheck_canada(notion, cfg, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
