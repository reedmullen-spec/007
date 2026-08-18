"""Framework selection — which SKILL.md drives the research pack, and which
persona titles the buying-group build uses.

Was AE-based until Aug 2026 (`enrichment.framework_by_ae`: Avi -> fieldatlas,
everyone else -> concretedna). That never actually worked as intended, because
`routing.py` never assigns Avi from geography — he isn't in `country_ae_map`,
`us_state_ae`, or `eu_default` — so the only paths to AE=avi were HubSpot
company ownership or a human hand-tagging the row. A UK modular prison found by
ingest resolved GB -> aled -> concretedna and got a concrete-sensing pack.

Now gated on the PROJECT instead (Reed's call). FieldAtlas fires only when ALL
THREE hold: the project is in the UK, it is at PCSA stage, and it shows a
DfMA/modular signal. See CLAUDE.md rule 20 for why it's an AND and what it
costs.

The same resolver must be used by the research path (enrich.py) and the
contacts path (contacts.py / actions.py), because the framework picks both the
system prompt AND the Amplemarket persona titles. If those two disagree you get
a modular research pack matched against concrete personas. Keeping the resolver
deterministic from (country, stage, text) is what lets both paths agree without
storing the choice anywhere.
"""
from __future__ import annotations

import re

DEFAULT_FRAMEWORK = "concretedna"
FIELDATLAS = "fieldatlas"


def dfma_signal(text: str, keywords: list[str]) -> bool:
    """Word-boundary keyword match, per CLAUDE.md rule 9.

    Boundary-matched rather than substring because the gate leans on short
    acronyms — a bare `in` test for "MMC" or "pod" hits inside unrelated words.
    Same matcher as news.py's headline gate.
    """
    lowered = (text or "").lower()
    return any(re.search(r"\b" + re.escape(k.lower()) + r"\b", lowered)
               for k in keywords or [])


def resolve_framework(cfg: dict, *, country: str = "", stage: str = "",
                      text: str = "") -> str:
    """Return the framework name for a project. Defaults to concretedna.

    `text` should be the richest description available (title + summary) —
    the DfMA signal is keyword-only, since `work_nature` has no modular
    option to test against.
    """
    enrichment = cfg.get("enrichment") or {}
    default = enrichment.get("default_framework", DEFAULT_FRAMEWORK)
    gate = enrichment.get("fieldatlas_gate") or {}

    if not gate or not gate.get("enabled", True):
        return default

    countries = [c.upper() for c in gate.get("countries") or []]
    if (country or "").strip().upper() not in countries:
        return default

    if (stage or "").strip() not in (gate.get("stages") or []):
        return default

    if not dfma_signal(text, gate.get("dfma_keywords") or []):
        return default

    return FIELDATLAS


def explain(cfg: dict, *, country: str = "", stage: str = "",
            text: str = "") -> str:
    """Human-readable reason for the choice — printed by the CLI paths so a
    surprising concretedna pack on an obviously-modular project is debuggable
    without reading this module."""
    enrichment = cfg.get("enrichment") or {}
    gate = enrichment.get("fieldatlas_gate") or {}
    if not gate or not gate.get("enabled", True):
        return "fieldatlas gate disabled"
    countries = [c.upper() for c in gate.get("countries") or []]
    checks = [
        (f"country {country or '?'} in {countries}",
         (country or "").strip().upper() in countries),
        (f"stage {stage or '?'} is PCSA",
         (stage or "").strip() in (gate.get("stages") or [])),
        ("DfMA keyword hit",
         dfma_signal(text, gate.get("dfma_keywords") or [])),
    ]
    failed = [label for label, ok in checks if not ok]
    if failed:
        return "concretedna — gate closed on: " + "; ".join(failed)
    return "fieldatlas — UK + PCSA + DfMA all matched"
