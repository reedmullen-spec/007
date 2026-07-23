"""Amplemarket client — step 3 buying-group build.

Verified against docs.amplemarket.com:
  POST /people/search  (company_names, person_titles, person_seniorities,
                        person_keywords, page_size capped at 100)
  POST /lead-lists     (required: shared, owner email, leads, type=linkedin;
                        leads need linkedin_url; 202 response has url + id)
"""
from __future__ import annotations

import requests

BASE = "https://api.amplemarket.com"


class AmplemarketClient:
    def __init__(self, api_key: str, cfg: dict):
        self.cfg = cfg["amplemarket"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _check(self, resp: requests.Response) -> dict:
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Amplemarket API failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def search_people(self, *, company_names: list[str], titles: list[str],
                      keywords: list[str] | None = None,
                      locations: list[str] | None = None,
                      limit: int = 20) -> list[dict]:
        body: dict = {
            "company_names": company_names,
            "person_titles": titles,
            "page": 1,
            "page_size": min(limit, 100),
        }
        if keywords:
            body["person_keywords"] = keywords
        if locations:
            body["person_locations"] = locations
        data = self._check(self.session.post(f"{BASE}/people/search",
                                             json=body, timeout=60))
        return data.get("results", [])

    def create_lead_list(self, *, name: str, people: list[dict]) -> dict:
        leads = [
            {
                "linkedin_url": p["linkedin_url"],
                "title": p.get("title") or "",
                "company_name": (p.get("company") or {}).get("name", ""),
            }
            for p in people if p.get("linkedin_url")
        ]
        if not leads:
            raise RuntimeError("No leads with LinkedIn URLs to create a list from")
        body = {
            "name": name[:120],
            "shared": self.cfg.get("list_shared", True),
            "owner": self.cfg["owner_email"],
            "type": "linkedin",
            "options": self.cfg.get("options", {}),
            "leads": leads,
        }
        return self._check(self.session.post(f"{BASE}/lead-lists",
                                             json=body, timeout=60))
