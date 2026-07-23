"""Anthropic Messages API client — runs the step-2 background research.

POST https://api.anthropic.com/v1/messages with the ConcreteDNA or
FieldAtlas framework as the system prompt and web search enabled, returning
a markdown research pack.
"""
from __future__ import annotations

from pathlib import Path

import requests

API_URL = "https://api.anthropic.com/v1/messages"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

FALLBACK_FRAMEWORK = """You are a construction-market research assistant for
Converge (concrete sensing: ConcreteDNA — Signal/Cure embedded sensors, Helix,
Data Hub, MixAI; and FieldAtlas — BLE component tracking for modular/DfMA).
Produce a concise background pack for the project below: current stage and
timeline, concrete scope and pour window, contractor/JV structure (resolve JV
entities, decompose into sub-lots where relevant), client goals (especially
sustainability / low-carbon-concrete targets), recent news, and which Converge
products fit and why. Use headers. Be factual; cite sources inline as URLs.
Flag anything unverified."""


def _load_framework(framework: str) -> str:
    """Use the repo's SKILL.md for the framework if present, else fallback."""
    path = SKILLS_DIR / framework / "SKILL.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return FALLBACK_FRAMEWORK


def run_research(api_key: str, cfg: dict, *, title: str, country: str,
                 framework: str, notice_url: str = "") -> str:
    e = cfg["enrichment"]
    user_prompt = (
        f"Project: {title}\nCountry: {country or 'unknown'}\n"
        f"Source notice: {notice_url or 'n/a'}\n\n"
        f"Research this project and produce the background pack."
    )
    body = {
        "model": e["anthropic_model"],
        "max_tokens": e.get("max_tokens", 4000),
        "system": _load_framework(framework),
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    resp = requests.post(
        API_URL,
        json=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=600,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API failed ({resp.status_code}): {resp.text[:400]}")
    data = resp.json()
    text = "\n".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Anthropic API returned no text content")
    return text
