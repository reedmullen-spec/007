"""Client for the UK Find a Tender Service (FTS) OCDS API.

Endpoint : GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages
Auth     : none
Params   : updatedFrom, updatedTo, stages, limit, cursor
Format   : OCDS 1.1 JSON release packages; follow links.next until absent.
"""
from __future__ import annotations

import datetime as dt

import requests

from ..models import Project

BASE_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"


def fetch(cfg: dict, days_back: int = 2, max_pages: int = 6,
          session: requests.Session | None = None) -> list[Project]:
    session = session or requests.Session()
    now = dt.datetime.now(dt.timezone.utc)
    params = {
        "updatedFrom": (now - dt.timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S"),
        "updatedTo": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "stages": "tender,award",
        "limit": 100,
    }

    projects: list[Project] = []
    url: str | None = BASE_URL
    first = True

    for _ in range(max_pages):
        if url is None:
            break
        resp = session.get(url, params=params if first else None, timeout=60)
        first = False
        if resp.status_code != 200:
            raise RuntimeError(f"FTS fetch failed ({resp.status_code}): {resp.text[:500]}")
        pkg = resp.json()
        for release in pkg.get("releases", []):
            proj = _parse_release(release)
            if proj:
                projects.append(proj)
        url = (pkg.get("links") or {}).get("next")

    return projects


def _parse_release(rel: dict) -> Project | None:
    tender = rel.get("tender") or {}
    title = tender.get("title") or rel.get("title") or ""
    if not title:
        return None

    ocid = rel.get("ocid") or rel.get("id") or "unknown"
    buyer = ((rel.get("buyer") or {}).get("name")) or ""

    cpv: list[str] = []
    classification = tender.get("classification") or {}
    if classification.get("id"):
        cpv.append(str(classification["id"]))
    for item in tender.get("items", []) or []:
        cid = (item.get("classification") or {}).get("id")
        if cid:
            cpv.append(str(cid))
        for extra in item.get("additionalClassifications", []) or []:
            if extra.get("id"):
                cpv.append(str(extra["id"]))

    value = (tender.get("value") or {}).get("amount")
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None

    deadline = (tender.get("tenderPeriod") or {}).get("endDate", "")

    return Project(
        source="FTS",
        notice_id=str(ocid),
        title=str(title),
        url=(f"https://www.find-tender.service.gov.uk/Search/Results?keywords={ocid}"
             if ocid != "unknown" else "https://www.find-tender.service.gov.uk/"),
        buyer=str(buyer),
        country="GB",
        cpv_codes=cpv,
        value=value,
        currency="GBP",
        deadline=str(deadline or ""),
    )
