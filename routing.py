"""Two-tier AE resolver.

Tier 1 — HubSpot ownership. If the company (buyer for tenders, entity for
news) exists in HubSpot, inherit its live hubspot_owner_id. This handles
entity-level nuance (e.g. bam.nl -> Lisa while BAM Contractors IE -> Aled).
The country field in HubSpot is unreliable; hubspot_owner_id is the truth.

Tier 2 — geography heuristics from config:
  * UK -> Aled, unless the company is a European-owned contractor -> Lisa
  * EU -> Lisa, Italy -> Aled
"""
from __future__ import annotations

from .hubspot_client import HubSpotClient


def _owner_id_to_ae(owner_id: str, cfg: dict) -> str | None:
    for ae, oid in cfg["hubspot"]["owners"].items():
        if str(oid) == str(owner_id) and ae != "reed":
            return ae
    return None


def resolve_ae(company_name: str, country: str, cfg: dict,
               hubspot: HubSpotClient | None = None) -> str:
    routing = cfg["routing"]

    # Tier 1: live HubSpot ownership.
    if hubspot is not None and routing.get("use_hubspot_owner_lookup") and company_name:
        try:
            owner_id = hubspot.find_company_owner(company_name)
            if owner_id:
                ae = _owner_id_to_ae(owner_id, cfg)
                if ae:
                    return ae
        except Exception:
            pass  # never let a lookup failure block the card

    # Tier 2: geography.
    country = (country or "").upper()
    name_lower = (company_name or "").lower()

    # North America / Australia: no AE split defined yet — no suggestion,
    # deals default to Reed as owner.
    if country in [c.upper() for c in routing.get("no_ae_countries", [])]:
        return ""

    if country == "GB":
        for needle in routing.get("uk_european_owned_overrides", []):
            if needle.lower() in name_lower:
                return "lisa"
        return routing["country_ae_map"].get("GB", "aled")

    return routing["country_ae_map"].get(country, routing.get("eu_default", "lisa"))
