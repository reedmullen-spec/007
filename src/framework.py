"""Framework selection — which SKILL.md drives the research pack, and which
persona titles the buying-group build uses.

Was AE-based until Aug 2026 (`enrichment.framework_by_ae`: Avi -> fieldatlas,
everyone else -> concretedna). That never actually worked as intended, because
`routing.py` never assigns Avi from geography — he isn't in `country_ae_map`,
`us_state_ae`, or `eu_default` — so the only paths to AE=avi were HubSpot
company ownership or a human hand-tagging the row. A UK modular prison found by
ingest resolved GB -> aled -> concretedna and got a concrete-sensing pack.

Now gated on the PROJECT instead (Reed's call). FieldAtlas fires only when ALL
THREE hold: the project is in the UK, it is at PCSA stage, and it is flagged as
DfMA/modular. See CLAUDE.md rule 20 for why it's an AND and what it costs.

Two things about the signals, both learned by checking the live database
against 893 real rows rather than reasoning about the code (the first version
of this module got both wrong and shipped a gate that matched zero rows):

  * **Region, not Country.** `Country` is NOT normalised in Notion — it holds
    both "GB" and "United Kingdom", and likewise US/United States,
    CA/Canada, AU/Australia, DE/Germany. `Region` is a canonical select, so
    it is the reliable UK test. Country is still accepted as a fallback for
    CLI paths that have no row, normalised through `country_aliases`.
  * **DfMA is a structured field, not a keyword.** `Use case` (multi-select)
    has a canonical "DfMA / modular" option set by qualify() at ingest, and
    `Fit profile` has "Industrialised construction / DfMA". Keywords are a
    fallback only: the one genuinely qualifying row in the database (Bouygues
    UK / Cambridge Children's Hospital) carries "DfMA / modular" in `Use
    case` while its title and Summary contain no DfMA keyword at all.

The same resolver must be used by the research path (enrich.py) and the
contacts path (contacts.py / actions.py), because the framework picks both the
system prompt AND the Amplemarket persona titles. If those two disagree you get
a modular research pack matched against concrete personas. Keeping the resolver
deterministic from the row's fields is what lets both paths agree without
storing the choice anywhere.
"""
from __future__ import annotations

import json
import re

DEFAULT_FRAMEWORK = "concretedna"
FIELDATLAS = "fieldatlas"


def _as_list(value) -> list[str]:
    """Accept a Notion multi-select however the caller happens to hold it: a
    real list, the JSON-array text the SQL view returns, or a bare string."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (ValueError, TypeError):
            pass
    return [text]


def dfma_signal(text: str, keywords: list[str]) -> bool:
    """Word-boundary keyword match, per CLAUDE.md rule 9.

    Boundary-matched rather than substring because the list leans on short
    acronyms — a bare `in` test for "MMC" hits inside unrelated words, and
    "commmodular" would match "modular". Same matcher as news.py's headline
    gate. This is a FALLBACK signal; prefer the structured fields.
    """
    lowered = (text or "").lower()
    return any(re.search(r"\b" + re.escape(k.lower()) + r"\b", lowered)
               for k in keywords or [])


def _region_ok(gate: dict, region: str, country: str) -> bool:
    regions = [r.lower() for r in gate.get("regions") or []]
    if (region or "").strip().lower() in regions:
        return True
    # No Region (CLI paths with only a country string): normalise the country.
    aliases = gate.get("country_aliases") or {}
    c = (country or "").strip().casefold()
    if not c:
        return False
    for target_region, names in aliases.items():
        if target_region.lower() not in regions:
            continue
        if any(c == str(n).strip().casefold() for n in names or []):
            return True
    return False


def _dfma_ok(gate: dict, use_cases, fit_profile: str, text: str) -> bool:
    wanted_uc = {str(u).casefold() for u in gate.get("dfma_use_cases") or []}
    if wanted_uc & {u.strip().casefold() for u in _as_list(use_cases)}:
        return True
    wanted_fp = {str(f).casefold() for f in gate.get("dfma_fit_profiles") or []}
    if (fit_profile or "").strip().casefold() in wanted_fp and wanted_fp:
        return True
    # Fallback for rows whose Use case is Unknown/blank.
    return dfma_signal(text, gate.get("dfma_keywords") or [])


def resolve_framework(cfg: dict, *, region: str = "", country: str = "",
                      stage: str = "", use_cases=None, fit_profile: str = "",
                      text: str = "") -> str:
    """Return the framework name for a project. Defaults to concretedna.

    Pass `region` when you have the row (it is canonical); `country` is a
    fallback for CLI paths. `use_cases` / `fit_profile` are the DfMA signal;
    `text` (title + Summary) is only the keyword fallback.
    """
    enrichment = cfg.get("enrichment") or {}
    default = enrichment.get("default_framework", DEFAULT_FRAMEWORK)
    gate = enrichment.get("fieldatlas_gate") or {}

    if not gate or not gate.get("enabled", True):
        return default
    if not _region_ok(gate, region, country):
        return default
    if (stage or "").strip() not in (gate.get("stages") or []):
        return default
    if not _dfma_ok(gate, use_cases, fit_profile, text):
        return default
    return FIELDATLAS


def explain(cfg: dict, *, region: str = "", country: str = "", stage: str = "",
            use_cases=None, fit_profile: str = "", text: str = "") -> str:
    """Human-readable reason for the choice — printed by the CLI paths so a
    surprising concretedna pack on an obviously-modular project is debuggable
    without reading this module."""
    enrichment = cfg.get("enrichment") or {}
    gate = enrichment.get("fieldatlas_gate") or {}
    if not gate or not gate.get("enabled", True):
        return "fieldatlas gate disabled"
    checks = [
        (f"region {region or country or '?'} not in {gate.get('regions') or []}",
         _region_ok(gate, region, country)),
        (f"stage {stage or '?'} is not PCSA",
         (stage or "").strip() in (gate.get("stages") or [])),
        ("no DfMA signal (Use case / Fit profile / keyword)",
         _dfma_ok(gate, use_cases, fit_profile, text)),
    ]
    failed = [label for label, ok in checks if not ok]
    if failed:
        return "concretedna — gate closed on: " + "; ".join(failed)
    return "fieldatlas — UK + PCSA + DfMA all matched"
