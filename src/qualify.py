"""Cheap qualify pass — one small API call per candidate project.

Reads the notice/headline data and returns structured observations:
canonical GC, project type, work nature, project stage, expected concrete
start, best-guess location, use cases, and product fit. No web search — it
works from what the source gave us; the deep research step is where
searching happens.

Fit is NOT decided here — src/scoring.py computes it deterministically from
these observations, so the same project always lands in the same band. The
model's job is accurate observation only.
"""
from __future__ import annotations

import json

import requests

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = """You qualify construction projects for Converge (concrete sensing:
embedded maturity/thermal sensors, QA data platform, mix carbon optimisation;
plus DfMA component tracking). You are given raw data about one project
(tender notice or news headline). Return ONLY a JSON object, no markdown,
with exactly these keys:

summary: 2-3 sentences, what this project is, plain language.
general_contractor: canonical PARENT company name (e.g. "Balfour Beatty" not
  "Balfour Beatty Ground Engineering Ltd"; JV entity name if a JV). "" if unknown.
client: the commissioning client, developer or authority. "" if unknown.
jv_parents: if the contractor is a JV, its parent companies comma-separated,
  else "".
project_type: exactly one of {types}.
work_nature: exactly one of {natures}.
project_stage: exactly one of {stages}.
use_case: array of zero or more of {use_cases}.
product_fit: array of zero or more of {products}. Rules: mass concrete or
  in-situ frame -> "Cure / Signal" + "Data Hub"; DfMA/modular -> "FieldAtlas";
  stated low-carbon or net-zero target -> "MixAI"; embedding impossible ->
  "Helix".
concrete_opportunity: "Small" | "Medium" | "Large" | "Unknown" — the size of
  OUR prize (concrete volume), not the project's headline value.
expected_concrete_start: best estimate like "Q3 2027" or "Live" or "Unknown".
expected_completion: best estimate of practical completion, like "Q3 2028"
  or "2029" or "Unknown".
location: most specific place you can infer (site, town, or region). "" if none.
competitor: any rival monitoring/curing system named or implied, else "".

Do NOT score the project — fit is computed downstream from these
observations by src/scoring.py, so it stays reproducible. Your job is
accurate observation only.

Judge only from the given data. Unknown is a valid answer; never invent."""


def qualify(api_key: str, cfg: dict, *, title: str, source: str,
            country: str, buyer: str = "", value: str = "",
            url: str = "", article_text: str = "", notes: str = "") -> dict:
    """article_text: full page text when the source gives us more than a
    bare notice — the manual-entry paths (URL-to-project, bulk import)
    fetch this via src/fetch_content.py so the model works from a real
    article instead of just a title. notes: free-text human context (the
    intake form's Notes field) — a person's own knowledge, kept distinct
    from the fetched article text."""
    from .notion_client import NotionClient as _N
    quote = lambda xs: ", ".join(f'"{x}"' for x in xs)
    user = (f"Source: {source}\nTitle: {title}\nCountry: {country}\n"
            f"Buyer/entity: {buyer or 'unknown'}\nValue: {value or 'unknown'}\n"
            f"URL: {url or 'n/a'}")
    if article_text:
        user += f"\n\nFull article text:\n{article_text}"
    if notes:
        user += f"\n\nSubmitter's own notes:\n{notes}"
    body = {
        "model": cfg["ingest"]["qualify_model"],
        "max_tokens": 500,
        "system": (SYSTEM.replace("{types}", quote(_N.PROJECT_TYPES))
                   .replace("{natures}", quote(_N.WORK_NATURES))
                   .replace("{stages}", quote(_N.PROJECT_STAGES))
                   .replace("{use_cases}", quote(_N.USE_CASES))
                   .replace("{products}", quote(_N.PRODUCTS))),
        "messages": [{"role": "user", "content": user}],
    }
    resp = requests.post(API_URL, json=body, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Qualify call failed ({resp.status_code}): {resp.text[:300]}")
    text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                   if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)

    # Defensive normalisation against the canonical lists. Off-list values
    # coerce rather than fail — a wrong option name breaks the Notion write.
    if data.get("project_type") not in _N.PROJECT_TYPES:
        data["project_type"] = "Other"
    if data.get("work_nature") not in _N.WORK_NATURES:
        data["work_nature"] = "Unknown"
    if data.get("project_stage") not in _N.PROJECT_STAGES:
        data["project_stage"] = "Unknown"
    if data.get("concrete_opportunity") not in ("Small", "Medium", "Large", "Unknown"):
        data["concrete_opportunity"] = "Unknown"
    data["use_case"] = [u for u in (data.get("use_case") or [])
                        if u in _N.USE_CASES] or ["Unknown"]
    data["product_fit"] = [p for p in (data.get("product_fit") or [])
                           if p in _N.PRODUCTS]
    for key in ("summary", "general_contractor", "client", "jv_parents",
                "location", "competitor", "expected_concrete_start",
                "expected_completion"):
        data.setdefault(key, "")
    return data
