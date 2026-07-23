"""Client for the SAM.gov Get Opportunities API v2 (US federal procurement).

Endpoint : GET https://api.sam.gov/opportunities/v2/search
Auth     : api_key query parameter — free key from any sam.gov account
           (Account Details -> API Key). Secret name: SAM_API_KEY.
Params   : postedFrom/postedTo (MM/dd/yyyy), ncode (NAICS), ptype, limit.

Covers FEDERAL work only (USACE civil works, VA hospitals, GSA, military
construction). The US commercial market lives in Dodge/ConstructConnect.

Cards are routed east/west by place of performance state, so Jamie and
Alex each see their side.
"""
from __future__ import annotations

import datetime as dt
import os

import requests

from ..models import Project

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"

# ptype: o=solicitation, p=presolicitation, k=combined synopsis, a=award
PTYPES = ["o", "p", "k", "a"]

WEST_STATES = {"WA", "OR", "CA", "NV", "ID", "MT", "WY", "UT", "CO", "AZ",
               "NM", "AK", "HI"}


def fetch(cfg: dict, days_back: int = 2,
          session: requests.Session | None = None) -> list[Project]:
    api_key = os.environ.get("SAM_API_KEY", "")
    if not api_key:
        print("SAM.gov: SAM_API_KEY not set — skipping US federal tenders. "
              "Get a free key at sam.gov (Account Details -> API Key) and add "
              "it as a GitHub secret named SAM_API_KEY.")
        return []

    session = session or requests.Session()
    now = dt.date.today()
    posted_from = (now - dt.timedelta(days=days_back)).strftime("%m/%d/%Y")
    posted_to = now.strftime("%m/%d/%Y")

    projects: list[Project] = []
    seen_ids: set[str] = set()

    for naics in cfg["filters"].get("naics_codes", []):
        params = {
            "api_key": api_key,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "ncode": naics,
            "ptype": ",".join(PTYPES),
            "limit": 100,
        }
        resp = session.get(SEARCH_URL, params=params, timeout=60)
        if resp.status_code == 429:
            print("SAM.gov: daily rate limit hit — partial results this run.")
            break
        if resp.status_code != 200:
            raise RuntimeError(f"SAM.gov fetch failed ({resp.status_code}): {resp.text[:400]}")
        for opp in resp.json().get("opportunitiesData", []):
            proj = _parse_opportunity(opp)
            if proj and proj.notice_id not in seen_ids:
                seen_ids.add(proj.notice_id)
                projects.append(proj)

    return projects


def _parse_opportunity(opp: dict) -> Project | None:
    title = opp.get("title") or ""
    if not title:
        return None

    notice_id = opp.get("noticeId") or opp.get("solicitationNumber") or "unknown"
    agency = opp.get("fullParentPathName") or opp.get("department") or ""
    naics = str(opp.get("naicsCode") or "")

    value = None
    award = opp.get("award") or {}
    awardee = (award.get("awardee") or {}).get("name", "")
    if award.get("amount"):
        try:
            value = float(str(award["amount"]).replace(",", ""))
        except (TypeError, ValueError):
            value = None

    state = ""
    pop = opp.get("placeOfPerformance") or {}
    if isinstance(pop.get("state"), dict):
        state = pop["state"].get("code", "") or ""

    display_title = f"{awardee} — {title}" if awardee else title

    proj = Project(
        source="SAM",
        notice_id=str(notice_id),
        title=display_title,
        url=opp.get("uiLink") or "https://sam.gov/",
        buyer=awardee or agency,
        country="US",
        cpv_codes=[naics] if naics else [],
        value=value,
        currency="USD",
        deadline=str(opp.get("responseDeadLine") or ""),
    )
    # east/west side for channel + mention routing
    proj.us_side = "us_west" if state in WEST_STATES else "us_east"  # type: ignore[attr-defined]
    return proj
