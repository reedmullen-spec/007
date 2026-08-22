"""HubSpot CRM v3 client.

Responsibilities:
  * ensure the tender_notice_id custom deal property exists (one-off bootstrap)
  * dedup pre-check: search deals on tender_notice_id (CRM = source of truth)
  * create deals in Sales Pipeline / Identified with the notice id stamped
  * create leads against the general contractor's company record, same
    notice-id dedup key as deals
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

Extra scopes the Create lead action needs, and ONLY that action (see
actions.py's _run_create_lead for why they are never touched by a run that
doesn't use them):
  crm.objects.leads.read, crm.objects.leads.write,
  crm.schemas.leads.write (notice-id property bootstrap on the lead object),
  crm.objects.companies.write (find_or_create_company — the rest of the
  client only ever reads companies)
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

    def ensure_lead_notice_property(self) -> None:
        """Create the notice-id property on the LEAD object if it's missing.

        Same name and same job as the deal property (rule #2: HubSpot is
        dedup truth, keyed on the notice id) but a genuinely separate
        property — HubSpot property definitions do not span object types.

        Deliberately NOT called from actions.py's preamble like its deal
        sibling: a portal without the leads scopes would raise here and take
        down Enrich/Create deal/Build contacts too. Bootstrapped lazily by
        the one action that needs it instead.

        `lead_property_group` is a config knob because the leads object's
        default property-group name could not be read back from the portal
        (the token has no leads scope yet) — a 400 naming groupName here is
        that guess being wrong, not a scope problem.
        """
        self._ensure_property(
            self.cfg["notice_id_property"], "Tender notice ID",
            "Stable dedup key stamped by 007 tender-radar.",
            object_type="leads",
            group_name=self.cfg.get("lead_property_group", "leadinformation"))

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
    def _owner_id(self, ae: str | None) -> str:
        """Owner for a newly created object: the resolved AE when
        deal_owner_mode says so and they're a known owner, else reed.
        Shared by create_deal/create_lead — an AE who owns the deal but not
        the lead on the same project is exactly the kind of split this
        codebase keeps getting bitten by (see the GC/JV drift, rule #14)."""
        owners = self.cfg["owners"]
        if self.cfg.get("deal_owner_mode") == "ae" and ae and ae in owners:
            return owners[ae]
        return owners["reed"]

    def create_deal(self, name: str, notice_id: str, ae: str | None,
                    summary: str | None = None) -> dict:
        owner_id = self._owner_id(ae)

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

    # --------------------------------------------------------------- leads
    def find_lead_by_notice_id(self, notice_id: str) -> dict | None:
        """The existing lead for this notice, or None — the lead-side twin of
        find_deal_by_notice_id, so a re-ticked Create lead box is a no-op
        rather than a second lead (rule #2 applied to the lead object)."""
        body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": self.cfg["notice_id_property"],
                    "operator": "EQ",
                    "value": notice_id,
                }]
            }],
            "properties": ["hs_lead_name", self.cfg["notice_id_property"]],
            "limit": 1,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/leads/search", json=body, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def create_lead(self, name: str, notice_id: str, ae: str | None,
                    company_id: str) -> dict:
        """Create a lead against the general contractor's company record.

        `company_id` is required, not optional: HubSpot refuses a lead with
        no primary contact or company association, so there is no useful
        "just make the lead" fallback to offer — the caller resolves the
        company first (find_or_create_company).

        hs_pipeline/hs_pipeline_stage/hs_lead_type are sent only when
        configured. The portal's lead pipeline IDs could not be read (no
        leads scope on the token as of Aug 2026), so the default is to let
        HubSpot drop the lead in its own default pipeline stage rather than
        POST a guessed ID and fail the whole action on it. Pin them in
        config.yaml once they're known.
        """
        properties = {
            "hs_lead_name": name[:250],
            "hubspot_owner_id": self._owner_id(ae),
            self.cfg["notice_id_property"]: notice_id,
        }
        for key, cfg_key in (("hs_pipeline", "lead_pipeline_id"),
                             ("hs_pipeline_stage", "lead_stage_id"),
                             ("hs_lead_type", "lead_type")):
            if self.cfg.get(cfg_key):
                properties[key] = self.cfg[cfg_key]

        r = self.session.post(
            f"{BASE}/crm/v3/objects/leads",
            json={"properties": properties,
                  "associations": [{
                      "to": {"id": company_id},
                      "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                 "associationTypeId": 610}],  # lead -> company
                  }]},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Lead creation failed ({r.status_code}): {r.text[:400]}")
        lead = r.json()
        lead["portal_url"] = (
            f"https://app.hubspot.com/contacts/{self.cfg['portal_id']}"
            f"/record/0-136/{lead.get('id')}"
        )
        return lead

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

    def find_or_create_company(self, name: str) -> dict:
        """The company record for a contractor name, creating it if absent.

        Name is the only key available here — the GC comes off a Notion row
        as text, with no domain — so this cannot use HubSpot's own
        domain-based company dedup. That matters more than it looks: the
        same contractor (Bouygues, Balfour Beatty) is the GC on dozens of
        tender rows, and a name search that misses would mint a company per
        row. Hence two attempts before creating anything: an exact EQ on
        `name`, then the fuzzy `query` search accepting only a
        case-insensitive exact name hit (the same search find_company_owner
        uses, whose scoring can return the right record where EQ's exact
        string match doesn't).

        Pass the primary contractor, not the raw 'General contractor/JV'
        field — src/deal_naming.primary_contractor() strips the '(JV: ...)'
        parenthetical that would otherwise become part of the company name.
        """
        name = (name or "").strip()
        if not name:
            raise RuntimeError("Cannot resolve a HubSpot company from a blank name.")

        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "name", "operator": "EQ", "value": name}]}],
            "properties": ["name"],
            "limit": 1,
        }
        r = self.session.post(f"{BASE}/crm/v3/objects/companies/search",
                              json=body, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            return results[0]

        r = self.session.post(f"{BASE}/crm/v3/objects/companies/search",
                              json={"query": name, "properties": ["name"],
                                    "limit": 5}, timeout=30)
        if r.status_code == 200:
            for result in r.json().get("results", []):
                found = ((result.get("properties") or {}).get("name") or "").strip()
                if found.lower() == name.lower():
                    return result

        r = self.session.post(f"{BASE}/crm/v3/objects/companies",
                              json={"properties": {"name": name[:200]}}, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Company creation failed ({r.status_code}): "
                               f"{r.text[:300]}. Needs crm.objects.companies.write "
                               f"on the private app.")
        company = r.json()
        print(f"Created HubSpot company {company.get('id')} for '{name}'.")
        return company
