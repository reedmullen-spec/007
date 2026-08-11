"""Client for CanadaBuys federal tender notices.

Unlike TED/FTS/AusTender, this is NOT a live-query REST API — CanadaBuys
publishes static, unauthenticated CSV files refreshed every 2 hours during
business hours. No API key, no pagination, no rate limits; "fetch" here
just means downloading the current snapshot of open tenders.

Endpoint: https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv

NOTE: verified live (Aug 2026) — this file has NO disclosed estimated
value at all (no value/amount/cost column exists). Filters.
keep_unknown_value lets these rows through regardless; value only becomes
available once awarded, via the separate contract history dataset — see
recheck_awards.py's Canada reconciliation, which backfills General
contractor + Value together once a tender's deadline has passed.

UNSPSC codes come newline-separated, each prefixed with "*" (observed
live, e.g. "*30120000\\n*72141000\\n*72141003") — stripped and split here
into a plain list, matched the same way as AusTender's UNSPSC codes
(filters.unspsc_prefixes; UNSPSC is a global standard, same code space).
"""
from __future__ import annotations

import csv
import io

import requests

from ..models import Project

URL = "https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv"
# canadabuys.canada.ca 403s requests' default User-Agent (verified live,
# Aug 2026) — a bot-protection rule, not an auth requirement.
USER_AGENT = "Mozilla/5.0 (compatible; 007RadarBot/1.0)"


def fetch(cfg: dict, days_back: int = 2, session: requests.Session | None = None,
          **_ignored) -> list[Project]:
    """days_back and any historical-mode kwargs (scope/max_pages, used by
    the other sources) are accepted for interface parity with ingest.py's
    generic source loop but unused — this file is always "currently open
    tenders", there's no date-range query to make against a static CSV."""
    session = session or requests.Session()
    resp = session.get(URL, timeout=60, headers={"User-Agent": USER_AGENT})
    if resp.status_code != 200:
        raise RuntimeError(f"CanadaBuys fetch failed ({resp.status_code}): {resp.text[:400]}")

    projects: list[Project] = []
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    for row in reader:
        proj = _parse_row(row)
        if proj:
            projects.append(proj)
    return projects


def _parse_row(row: dict) -> Project | None:
    title = (row.get("title-titre-eng") or "").strip()
    if not title:
        return None

    ref = (row.get("referenceNumber-numeroReference") or "").strip()
    solicitation = (row.get("solicitationNumber-numeroSollicitation") or "").strip()
    notice_id = ref or solicitation
    if not notice_id:
        return None

    codes = [c.strip().lstrip("*") for c in
            (row.get("unspsc") or "").split("\n") if c.strip()]
    buyer = (row.get("contractingEntityName-nomEntitContractante-eng") or "").strip()
    url = (row.get("noticeURL-URLavis-eng") or "").strip() or "https://canadabuys.canada.ca/"
    deadline = (row.get("tenderClosingDate-appelOffresDateCloture") or "").strip()

    return Project(
        source="CANADA",
        notice_id=notice_id,   # exact match key reused by recheck_awards.py's award reconciliation
        title=title,
        url=url,
        buyer=buyer,
        country="CA",
        cpv_codes=codes,   # UNSPSC, matched via filters.unspsc_prefixes
        value=None,        # never disclosed pre-award — see module docstring
        currency="CAD",
        deadline=deadline[:10] if deadline else "",
    )
