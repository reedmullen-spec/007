"""Filter tender notices down to concrete-relevant candidates."""
from __future__ import annotations

from .models import Project


def _matches_codes(project: Project, f: dict) -> bool:
    """Classification match using the right code system per source:
    TED/FTS carry CPV, AusTender carries UNSPSC, SAM carries NAICS."""
    prefix_map = {
        "TED": f.get("cpv_prefixes", []),
        "FTS": f.get("cpv_prefixes", []),
        "AUSTENDER": f.get("unspsc_prefixes", []),
        "SAM": f.get("naics_codes", []),
    }
    prefixes = prefix_map.get(project.source, [])
    if not project.cpv_codes or not prefixes:
        return False
    return any(code.startswith(p) for code in project.cpv_codes for p in prefixes)


def _keyword_hit(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def _value_ok(project: Project, f: dict) -> bool:
    if project.value is None:
        return bool(f.get("keep_unknown_value", True))
    thresholds = {"GBP": f.get("min_value_gbp"), "EUR": f.get("min_value_eur"),
                  "AUD": f.get("min_value_aud"), "USD": f.get("min_value_usd")}
    threshold = thresholds.get(project.currency) or f["min_value_eur"]
    return project.value >= threshold


def filter_projects(projects: list[Project], cfg: dict) -> list[Project]:
    f = cfg["filters"]
    countries = set(f.get("countries", []))
    kept: list[Project] = []

    for p in projects:
        # TED-only country gate (FTS=UK, AUSTENDER=AU, SAM=US by definition).
        if p.source == "TED" and countries and p.country not in countries:
            continue
        # Relevance: classification match OR an include keyword in the title.
        if not (_matches_codes(p, f) or _keyword_hit(p.title, f["include_keywords"])):
            continue
        if _keyword_hit(p.title, f.get("exclude_keywords", [])):
            continue
        if not _value_ok(p, f):
            continue
        kept.append(p)

    return kept
