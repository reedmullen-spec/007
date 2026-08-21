"""HubSpot CRM v3 client.

Responsibilities:
  * ensure the tender_notice_id custom deal property exists (one-off bootstrap)
  * dedup pre-check: search deals on tender_notice_id (CRM = source of truth)
  * create deals in Sales Pipeline / Identified with the notice id stamped
  * company owner lookup for tier 1 of the AE resolver
  * push the Amplemarket buying group (contacts.py) into HubSpot as
    contacts, associated to the deal that gated the build

Private-app scopes needed:
  crm.objects.deals.read, crm.objects.deals.write,
  crm.schemas.deals.write (property bootstrap),
  crm.objects.notes.read, crm.objects.notes.write (pack/summary notes,
  and the association read that keeps them idempotent),
  crm.objects.companies.read (owner lookup),
  crm.objects.contacts.read, crm.objects.contacts.write,
  crm.schemas.contacts.write (linkedin_url property bootstrap)
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
    def _ensure_property(self, name: str, label: str, description: str,
                         field_type: str = "text", object_type: str = "deals",
                         group_name: str = "dealinformation") -> None:
        r = self.session.get(f"{BASE}/crm/v3/properties/{object_type}/{name}", timeout=30)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            r.raise_for_status()
        payload = {
            "name": name,
            "label": label,
            "type": "string",
            "fieldType": field_type,
            "groupName": group_name,
            "description": description,
        }
        create = self.session.post(f"{BASE}/crm/v3/properties/{object_type}",
                                   json=payload, timeout=30)
        if create.status_code not in (200, 201):
            raise RuntimeError(
                f"Could not create {object_type} property '{name}' "
                f"({create.status_code}): {create.text[:300]}. "
                f"Either add the crm.schemas.{object_type}.write scope to the private "
                f"app, or create the property manually in HubSpot settings."
            )

    def ensure_notice_property(self) -> None:
        """Create the tender_notice_id deal property if it doesn't exist."""
        self._ensure_property(self.cfg["notice_id_property"], "Tender notice ID",
                              "Stable dedup key stamped by 007 tender-radar.")

    def ensure_summary_property(self) -> None:
        """Create the enrichment-summary deal property if it doesn't exist.
        Holds the research pack's TL;DR so the deal is readable without
        opening the Notion pack — written by both directions of the
        enrich <-> create-deal actions (src/../enrich.py, actions.py),
        whichever runs first."""
        self._ensure_property(self.cfg["summary_property"], "007 enrichment summary",
                              "Research-pack TL;DR, stamped by 007 tender-radar.",
                              field_type="textarea")

    def ensure_linkedin_property(self) -> None:
        """Create the linkedin_url contact property if it doesn't exist —
        the identifier the Amplemarket buying-group contacts (contacts.py)
        are matched on, kept on the HubSpot contact once pushed there."""
        self._ensure_property("linkedin_url", "LinkedIn URL",
                              "Buying-group contact's LinkedIn profile, "
                              "stamped by 007 tender-radar.",
                              object_type="contacts", group_name="contactinformation")

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
    def create_deal(self, name: str, notice_id: str, ae: str | None,
                    summary: str | None = None) -> dict:
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
        if summary:
            properties[self.cfg["summary_property"]] = summary[:5000]
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

    # ------------------------------------------------------------ summary
    def update_deal_summary(self, deal_id: str, summary: str) -> None:
        """Backfill the enrichment TL;DR onto an already-existing deal —
        used when research runs after the deal was created."""
        r = self.session.patch(
            f"{BASE}/crm/v3/objects/deals/{deal_id}",
            json={"properties": {self.cfg["summary_property"]: summary[:5000]}},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Deal summary update failed ({r.status_code}): {r.text[:300]}")

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

    def deal_note_bodies(self, deal_id: str, limit: int = 100) -> list[str]:
        """The bodies of the notes already attached to a deal.

        Two documented calls rather than one search on `associations.deal`,
        which is not a supported filter for note objects. Raises on API
        failure so a caller using this purely as a dedup hint can decide for
        itself whether a missing answer means "write" or "skip" — a token
        without crm.objects.notes.read reaches here as a 403, not as [].
        """
        r = self.session.get(
            f"{BASE}/crm/v4/objects/deals/{deal_id}/associations/notes",
            params={"limit": limit},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Note association fetch failed "
                               f"({r.status_code}): {r.text[:300]}")
        ids = [a.get("toObjectId") for a in r.json().get("results", [])
               if a.get("toObjectId")]
        if not ids:
            return []
        r = self.session.post(
            f"{BASE}/crm/v3/objects/notes/batch/read",
            json={"properties": ["hs_note_body"],
                  "inputs": [{"id": str(i)} for i in ids]},
            timeout=30,
        )
        if r.status_code not in (200, 207):
            raise RuntimeError(f"Note batch read failed "
                               f"({r.status_code}): {r.text[:300]}")
        return [(n.get("properties") or {}).get("hs_note_body") or ""
                for n in r.json().get("results", [])]

    def deal_has_note_marker(self, deal_id: str, marker: str) -> bool:
        """Whether one of the deal's notes already carries `marker`."""
        return any(marker in body for body in self.deal_note_bodies(deal_id))

    # ------------------------------------------------------------ contacts
    def find_contact_by_email(self, email: str) -> dict | None:
        """Dedup pre-check, same pattern as find_deal_by_notice_id — email
        is the only field Amplemarket people are reliably matchable on."""
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname"],
            "limit": 1,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/contacts/search", json=body, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def upsert_contact(self, *, email: str = "", first_name: str = "",
                       last_name: str = "", title: str = "",
                       company_name: str = "", linkedin_url: str = "") -> dict:
        """Create or update (by email, if given) a buying-group contact.
        Without an email there's no reliable dedup key, so it's always a
        fresh create — acceptable here since re-running a buying-group
        build for the same project is rare, and a duplicate is harmless
        (never deleted, same as everything else in 007 — see CLAUDE.md)."""
        properties = {k: v for k, v in {
            "firstname": first_name, "lastname": last_name,
            "jobtitle": title, "company": company_name, "email": email,
            "linkedin_url": linkedin_url,
        }.items() if v}

        existing = self.find_contact_by_email(email) if email else None
        if existing:
            r = self.session.patch(f"{BASE}/crm/v3/objects/contacts/{existing['id']}",
                                   json={"properties": properties}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"Contact update failed ({r.status_code}): {r.text[:300]}")
            return r.json()

        r = self.session.post(f"{BASE}/crm/v3/objects/contacts",
                              json={"properties": properties}, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Contact creation failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def associate_default(self, from_type: str, from_id: str,
                          to_type: str, to_id: str) -> None:
        """Associate two objects using HubSpot's default association type
        for the pair (v4 API) — deliberately not a hardcoded numeric
        associationTypeId (see add_note's note->deal 214, which had to be
        found by testing): this endpoint resolves the right default itself."""
        r = self.session.put(
            f"{BASE}/crm/v4/objects/{from_type}/{from_id}/associations/default/"
            f"{to_type}/{to_id}", timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Association failed ({r.status_code}): {r.text[:300]}")

    # ------------------------------------------------------- owner lookup
    def find_company_owner(self, company_name: str) -> str | None:
        """Tier-1 AE resolution: live owner of the company record, if any.
        Cached per run — news runs look the same contractors up repeatedly."""
        cache = getattr(self, "_owner_cache", None)
        if cache is None:
            cache = self._owner_cache = {}
        key = company_name.lower().strip()
        if key in cache:
            return cache[key]
        body = {
            "query": company_name,
            "properties": ["name", "hubspot_owner_id"],
            "limit": 3,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/companies/search", json=body, timeout=30)
        if r.status_code != 200:
            cache[key] = None
            return None
        found = None
        for result in r.json().get("results", []):
            owner = (result.get("properties") or {}).get("hubspot_owner_id")
            if owner:
                found = str(owner)
                break
        cache[key] = found
        return found
