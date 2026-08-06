"""Shared qualify → geocode → route → score pipeline for the two manual-
entry paths (ingest.py's sweep_manual_rows, bulk_import.py). Factored out
after several rounds this session of the two paths silently drifting out
of sync (SDR, Fit reason, Expected completion each missed one path at
some point) — one function means new fields only need adding once.

Returns a plain dict of computed fields; callers do their own Notion
write (sweep_manual_rows updates an existing bare row via
update_properties, bulk_import.py creates a brand new one via
create_project_row) since those are genuinely different operations.
"""
from __future__ import annotations

import datetime as dt

from .fetch_content import fetch_page
from .geocode import geocode
from .qualify import normalize_observations, qualify
from .routing import resolve_ae
from .scoring import resolve_date, score_project

COUNTRY_REGION = {"GB": "uk", "US": "us", "CA": "ca", "AU": "au"}


def region_for_country(country: str) -> str:
    """Country -> the region bucket AE/partner routing expects. Shared by
    every manual-entry path (bulk import, intake sweep) so a country
    string only needs mapping to a region in one place."""
    return COUNTRY_REGION.get((country or "").upper(), "eu")


CATEGORICAL_KEYS = ("project_type", "work_nature", "project_stage")


def compute_fields(cfg: dict, api_key: str, *, title: str, source: str,
                   country: str = "", buyer: str = "", value: float | None = None,
                   currency: str = "EUR", url: str = "", region: str = "",
                   ae_override: str | None = None, notes: str = "",
                   known: dict | None = None) -> dict | None:
    """Fetches `url`'s article text (if given) to enrich the qualify call,
    then runs qualify -> geocode -> AE/SDR routing -> score. Returns None
    only if there's no usable title even after trying to derive one from
    the fetched page (i.e. nothing to qualify).

    ae_override: use this AE as-is instead of resolving one from
    gc/buyer/country. Manual rows are often hand-tagged with an AE already
    (White Cap/AE tips come in pre-tagged) — a fresh geography-based
    resolve would silently clobber that.

    known: pre-researched fields (bulk_import.py's optional CSV columns —
    project_type/work_nature/project_stage/use_case/gc/summary/
    concrete_start/completion). If the three categorical fields are all
    present, qualify() is skipped entirely for this row — a researcher who
    already determined the stage/type shouldn't need an API call to
    re-derive what they already know, and their own read is arguably more
    reliable than a model's guess from a bare title. Falls back to
    qualify() for anything left unknown."""
    have_categoricals = bool(known) and all(known.get(k) for k in CATEGORICAL_KEYS)

    article_text = ""
    if url and (not title or not have_categoricals):
        page = fetch_page(url)
        if not have_categoricals:
            article_text = page["text"]
        if not title:
            title = page["title"]
    if not title:
        return None

    if have_categoricals:
        q = normalize_observations({
            "project_type": known["project_type"],
            "work_nature": known["work_nature"],
            "project_stage": known["project_stage"],
            "use_case": known.get("use_case", []),
            "product_fit": known.get("product_fit", []),
            "concrete_opportunity": known.get("concrete_opportunity", "Unknown"),
            "expected_concrete_start": known.get("expected_concrete_start", ""),
            "expected_completion": known.get("expected_completion", ""),
            "summary": known.get("summary", ""),
            "general_contractor": known.get("gc", ""),
            "client": known.get("client", ""),
            "jv_parents": known.get("jv_parents", ""),
            "location": known.get("location", ""),
            "competitor": known.get("competitor", ""),
        })
    else:
        q = qualify(api_key, cfg, title=title, source=source, country=country,
                   buyer=buyer, value=(f"{value:,.0f} {currency}" if value else ""),
                   url=url, article_text=article_text, notes=notes)

    lat = lng = None
    if cfg["ingest"].get("geocode", True):
        coords = geocode(q.get("location", ""), country)
        if coords:
            lat, lng = coords

    gc = q.get("general_contractor", "")
    jv = q.get("jv_parents", "")
    ae = ae_override if ae_override is not None else (resolve_ae(gc or buyer, country, cfg, None) or "unassigned")
    sdr = cfg["routing"].get("ae_sdr_map", {}).get(ae, "")
    partner = ("Hakron" if country.upper() in
               [x.upper() for x in cfg.get("hakron_skip_contacts_countries", [])]
               else "White Cap" if region.startswith(("us", "ca")) else "TBD")

    scored = score_project({**q, "title": title, "gc": gc, "value": value,
                            "ae": ae, "partner_route": partner}, cfg)
    dimensions_str = "; ".join(
        f"{k}: {'ok' if v['pass'] else 'off'} — {v['note']}"
        for k, v in scored["dimensions"].items())
    completion = resolve_date(q.get("expected_completion", ""))

    return {
        "title": title, "location": q.get("location", ""), "gc": gc, "jv_parents": jv,
        "project_type": q.get("project_type", "Other"),
        "stage": q.get("project_stage", "Unknown"),
        "work_nature": q.get("work_nature", "Unknown"),
        "concrete_opportunity": q.get("concrete_opportunity", "Unknown"),
        "competitor": q.get("competitor", ""),
        "concrete_start": q.get("expected_concrete_start", ""),
        "completion_date": completion.isoformat() if completion else "",
        "summary": q.get("summary", ""), "client": q.get("client", "") or buyer,
        "use_case": q.get("use_case", []),
        "product_fit": scored["products"] or q.get("product_fit", []),
        "lat": lat, "lng": lng, "ae": ae, "sdr": sdr, "partner_route": partner,
        "fit": scored["fit"], "fit_profile": scored["profile"],
        "fit_reason": scored["reason"], "fit_dimensions": dimensions_str,
        "announced": dt.date.today().isoformat(),
    }
