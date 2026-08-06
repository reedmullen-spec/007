"""Client for the TED Europa Search API (v3).

Endpoint : POST https://api.ted.europa.eu/v3/notices/search
Auth     : none for published notices
Query    : TED expert query syntax
Paging   : paginationMode=ITERATION with iterationNextToken
"""
from __future__ import annotations

import datetime as dt

import requests

from ..models import Project

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{pubnum}"

# TED's expert query syntax requires ISO 3166-1 alpha-3 country codes
# (verified against the live API, Aug 2026) — config.yaml and the rest of
# the pipeline (filtering.py, _parse_notice below) use alpha-2 throughout,
# so the conversion happens only here, for the outbound query.
ALPHA2_TO_ALPHA3 = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "CH": "CHE", "CY": "CYP",
    "CZ": "CZE", "DE": "DEU", "DK": "DNK", "EE": "EST", "ES": "ESP",
    "FI": "FIN", "FR": "FRA", "GB": "GBR", "GR": "GRC", "HR": "HRV",
    "HU": "HUN", "IE": "IRL", "IS": "ISL", "IT": "ITA", "LI": "LIE",
    "LT": "LTU", "LU": "LUX", "LV": "LVA", "MT": "MLT", "NL": "NLD",
    "NO": "NOR", "PL": "POL", "PT": "PRT", "RO": "ROU", "SE": "SWE",
    "SI": "SVN", "SK": "SVK",
}

# Fields requested back from the search API.
FIELDS = [
    "publication-number",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "classification-cpv",
    "total-value",
    "estimated-value-lot",
    "deadline-receipt-tender-date-lot",
    "publication-date",
    # Award-notice-only fields (notice-type=can-standard, form-type=result):
    # populated when the winner is known, None on pre-award "competition"
    # notices — verified against the live API, Aug 2026. winner-name is
    # usually one name; organisation-name-tenderer lists every JV/consortium
    # member when there's more than one.
    "notice-type",
    "winner-name",
    "organisation-name-tenderer",
]


def _build_query(cfg: dict, days_back: int) -> str:
    f = cfg["filters"]
    since = (dt.date.today() - dt.timedelta(days=days_back)).strftime("%Y%m%d")
    cpv = " OR ".join(f"classification-cpv={p}*" for p in f["cpv_prefixes"])
    countries = " OR ".join(f"buyer-country={ALPHA2_TO_ALPHA3.get(c, c)}"
                            for c in f.get("countries", []))
    query = f"(publication-date>={since}) AND ({cpv})"
    if countries:
        query += f" AND ({countries})"
    return query


def fetch(cfg: dict, days_back: int = 2, max_pages: int = 4,
          session: requests.Session | None = None,
          scope: str = "ACTIVE") -> list[Project]:
    session = session or requests.Session()
    query = _build_query(cfg, days_back)
    projects: list[Project] = []
    next_token: str | None = None

    for _ in range(max_pages):
        body = {
            "query": query,
            "fields": FIELDS,
            "limit": 250,
            "scope": scope,
            "paginationMode": "ITERATION",
            "checkQuerySyntax": False,
        }
        if next_token:
            body["iterationNextToken"] = next_token

        resp = session.post(SEARCH_URL, json=body, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"TED search failed ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        notices = data.get("notices") or data.get("results") or []
        projects.extend(_parse_notice(n) for n in notices)

        next_token = data.get("iterationNextToken")
        if not next_token or not notices:
            break

    return projects


# --------------------------------------------------------------------------
# TED returns multilingual dict values and inconsistent field shapes; the
# helpers below flatten those defensively.

def _first_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return _first_text(val[0]) if val else ""
    if isinstance(val, dict):
        for key in ("eng", "en"):
            if key in val:
                return _first_text(val[key])
        return _first_text(next(iter(val.values()), ""))
    return str(val)


def _all_strings(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        out: list[str] = []
        for v in val:
            out.extend(_all_strings(v))
        return out
    if isinstance(val, dict):
        out = []
        for v in val.values():
            out.extend(_all_strings(v))
        return out
    return [str(val)]


def _to_float(val) -> float | None:
    try:
        if isinstance(val, (list, dict)):
            val = _first_text(val)
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_notice(n: dict) -> Project:
    pubnum = _first_text(n.get("publication-number") or n.get("publicationNumber") or n.get("ND"))
    title = _first_text(n.get("notice-title") or n.get("title-proc") or n.get("notice-title-glo"))
    buyer = _first_text(n.get("buyer-name") or n.get("organisation-name-buyer"))
    country = _first_text(n.get("buyer-country") or n.get("place-of-performance"))
    cpv = _all_strings(n.get("classification-cpv"))
    value = _to_float(n.get("total-value") or n.get("estimated-value-lot"))
    deadline = _first_text(n.get("deadline-receipt-tender-date-lot"))

    # Ground-truth winner, only present on award-type notices.
    winner = _first_text(n.get("winner-name"))
    tenderers = list(dict.fromkeys(_all_strings(n.get("organisation-name-tenderer"))))
    contractor = winner or (tenderers[0] if tenderers else "")
    jv_parents = ", ".join(tenderers) if len(tenderers) > 1 else ""

    return Project(
        source="TED",
        notice_id=pubnum or "unknown",
        title=title or "(untitled TED notice)",
        url=NOTICE_URL.format(pubnum=pubnum) if pubnum else "https://ted.europa.eu/",
        buyer=buyer,
        country=country.upper()[:2] if country else "",
        cpv_codes=cpv,
        value=value,
        currency="EUR",
        deadline=deadline,
        contractor=contractor,
        jv_parents=jv_parents,
    )
