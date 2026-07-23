"""HubSpot CRM v3 client.

Responsibilities:
  * ensure the tender_notice_id custom deal property exists (one-off bootstrap)
  * dedup pre-check: search deals on tender_notice_id (CRM = source of truth)
  * create deals in Sales Pipeline / Identified with the notice id stamped
  * company owner lookup for tier 1 of the AE resolver

Private-app scopes needed:
  crm.objects.deals.read, crm.objects.deals.write,
  crm.schemas.deals.write (property bootstrap),
  crm.objects.companies.read (owner lookup)
"""
from __future__ import annotations

import requests

BASE = "https://api.hubapi.com"


class HubSpotClient:
    def __init__(self, token: str, cfg: dict):
        self.cfg = cfg["hubspot"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------ property
    def ensure_notice_property(self) -> None:
        """Create the tender_notice_id deal property if it doesn't exist."""
        name = self.cfg["notice_id_property"]
        r = self.session.get(f"{BASE}/crm/v3/properties/deals/{name}", timeout=30)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            r.raise_for_status()
        payload = {
            "name": name,
            "label": "Tender notice ID",
            "type": "string",
            "fieldType": "text",
            "groupName": "dealinformation",
            "description": "Stable dedup key stamped by 007 tender-radar.",
        }
        create = self.session.post(f"{BASE}/crm/v3/properties/deals", json=payload, timeout=30)
        if create.status_code not in (200, 201):
            raise RuntimeError(
                f"Could not create deal property '{name}' "
                f"({create.status_code}): {create.text[:300]}. "
                f"Either add the crm.schemas.deals.write scope to the private "
                f"app, or create the property manually in HubSpot settings."
            )

    # --------------------------------------------------------------- dedup
    def find_deal_by_notice_id(self, notice_id: str) -> dict | None:
        """Return the existing deal (id + name) for this notice, or None."""
        body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": self.cfg["notice_id_property"],
                    "operator": "EQ",
                    "value": notice_id,
                }]
            }],
            "properties": ["dealname", self.cfg["notice_id_property"]],
            "limit": 1,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/deals/search", json=body, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    # -------------------------------------------------------------- create
    def create_deal(self, name: str, notice_id: str, ae: str | None) -> dict:
        owners = self.cfg["owners"]
        if self.cfg.get("deal_owner_mode") == "ae" and ae and ae in owners:
            owner_id = owners[ae]
        else:
            owner_id = owners["reed"]

        properties = {
            "dealname": name[:250],
            "pipeline": self.cfg["pipeline_id"],
            "dealstage": self.cfg["dealstage_id"],
            "hubspot_owner_id": owner_id,
            self.cfg["notice_id_property"]: notice_id,
        }
        r = self.session.post(
            f"{BASE}/crm/v3/objects/deals",
            json={"properties": properties},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Deal creation failed ({r.status_code}): {r.text[:400]}")
        deal = r.json()
        deal_id = deal.get("id")
        deal["portal_url"] = (
            f"https://app.hubspot.com/contacts/{self.cfg['portal_id']}/deal/{deal_id}"
        )
        return deal

    # ---------------------------------------------------------- deal read
    def get_deal(self, deal_id: str) -> dict:
        r = self.session.get(
            f"{BASE}/crm/v3/objects/deals/{deal_id}",
            params={"properties": f"dealname,hubspot_owner_id,"
                                  f"{self.cfg['notice_id_property']}"},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Deal fetch failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    # --------------------------------------------------------------- notes
    def add_note(self, deal_id: str, body_html: str, pin: bool = True) -> str:
        """Create a note on the deal; attempt to pin it (best effort)."""
        import datetime as _dt
        payload = {
            "properties": {
                "hs_note_body": body_html[:9000],
                "hs_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
            "associations": [{
                "to": {"id": deal_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 214}],  # note -> deal
            }],
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/notes", json=payload, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Note creation failed ({r.status_code}): {r.text[:300]}")
        note_id = r.json().get("id", "")
        if pin and note_id:
            # Pinning isn't a formally documented API surface; try, don't fail.
            try:
                self.session.patch(
                    f"{BASE}/crm/v3/objects/deals/{deal_id}",
                    json={"properties": {"hs_pinned_engagement_id": note_id}},
                    timeout=30,
                )
            except Exception:
                pass
        return note_id

    # ------------------------------------------------------- owner lookup
    def find_company_owner(self, company_name: str) -> str | None:
        """Tier-1 AE resolution: live owner of the company record, if any."""
        body = {
            "query": company_name,
            "properties": ["name", "hubspot_owner_id"],
            "limit": 3,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/companies/search", json=body, timeout=30)
        if r.status_code != 200:
            return None
        for result in r.json().get("results", []):
            owner = (result.get("properties") or {}).get("hubspot_owner_id")
            if owner:
                return str(owner)
        return None
