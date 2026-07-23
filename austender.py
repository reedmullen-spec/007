"""Client for the AusTender OCDS API (Australian federal procurement).

Endpoint : GET https://api.tenders.gov.au/ocds/findByDates/contractPublished/
           {fromISO}/{toISO}   (ISO-8601 datetimes, e.g. 2026-07-20T00:00:00Z)
Auth     : none
Format   : OCDS release packages.

NOTE: this API carries CONTRACT NOTICES (awards >= AU$10k) — the contractor
is already resolved, which suits project-first prospecting. It does NOT
carry open tenders (ATMs), and it's federal only: state-level projects
(NSW, VIC, QLD portals) are not included.
"""
from __future__ import annotations

import datetime as dt

import requests

from ..models import Project

BASE_URL = "https://api.tenders.gov.au/ocds/findByDates/contractPublished"


def fetch(cfg: dict, days_back: int = 2, max_pages: int = 5,
          session: requests.Session | None = None) -> list[Project]:
    session = session or requests.Session()
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    projects: list[Project] = []
    url: str | None = f"{BASE_URL}/{start}/{end}"

    for _ in range(max_pages):
        if url is None:
            break
        resp = session.get(url, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"AusTender fetch failed ({resp.status_code}): {resp.text[:400]}")
        pkg = resp.json()
        for release in pkg.get("releases", []):
            proj = _parse_release(release)
            if proj:
                projects.append(proj)
        links = pkg.get("links") or {}
        url = links.get("next")

    return projects


def _parse_release(rel: dict) -> Project | None:
    awards = rel.get("awards") or []
    tender = rel.get("tender") or {}
    title = tender.get("title") or rel.get("description") or ""

    supplier = ""
    value = None
    codes: list[str] = []

    if awards:
        award = awards[0]
        suppliers = award.get("suppliers") or []
        if suppliers:
            supplier = suppliers[0].get("name", "")
        value = (award.get("value") or {}).get("amount")
        for item in award.get("items", []) or []:
            cid = (item.get("classification") or {}).get("id")
            if cid:
                codes.append(str(cid))
            if not title:
                title = item.get("description", "")

    if not title:
        return None
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None

    ocid = rel.get("ocid") or rel.get("id") or "unknown"
    cn_id = str(ocid).split("-")[-1] if ocid != "unknown" else "unknown"

    return Project(
        source="AUSTENDER",
        notice_id=str(ocid),
        title=f"{supplier} — {title}" if supplier else str(title),
        url=(f"https://www.tenders.gov.au/Cn/Show/{cn_id}"
             if cn_id != "unknown" else "https://www.tenders.gov.au/"),
        buyer=supplier,   # for awards the supplier IS the routing target
        country="AU",
        cpv_codes=codes,  # UNSPSC codes, matched via unspsc_prefixes
        value=value,
        currency="AUD",
    )
