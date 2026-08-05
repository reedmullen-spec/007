"""Cheap qualify pass — one small API call per candidate project.

Reads the notice/headline data and returns structured fields: canonical GC,
project type (from the controlled list), phase, expected concrete start,
best-guess location, and a low/medium/high Converge fit with a one-line
reason. No web search — it works from what the source gave us; the deep
research step is where searching happens.
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
project_type: exactly one of {types}.
phase: one of "Tender" | "PCSA / preconstruction" | "Starting" | "On site" |
  "Finishing" | "Unknown".
expected_concrete_start: best estimate like "Q3 2027" or "Live" or "Unknown".
location: most specific place you can infer (site, town, or region). "" if none.
fit: "high" | "medium" | "low" — high = major in-situ concrete or DfMA scope
  with the pour/manufacture window still ahead; low = fit-out, refurb, small
  works, services-only, or concrete already finished.
fit_reason: one sentence explaining the rating.
client: the commissioning client/developer/authority. "" if unknown.
jv_parents: if the contractor is a JV, its parent companies comma-separated.
  "" otherwise.
concrete_scope: array of zero or more of {scopes}.
product_fit: array of zero or more of "Cure / Signal", "Data Hub", "MixAI",
  "FieldAtlas" — which Converge products plausibly fit and why the fit field
  says what it says. DfMA/modular projects -> FieldAtlas; mass concrete or
  in-situ frames -> Cure / Signal + Data Hub; stated low-carbon targets -> MixAI.

Judge only from the given data. Unknown is a valid answer; never invent."""

SCOPES = ["In-situ frame", "Mass concrete", "Piling / foundations", "Precast",
          "Tunnel linings", "Marine / water-retaining", "DfMA / modular",
          "Slabs / pavements"]
PRODUCTS = ["Cure / Signal", "Data Hub", "MixAI", "FieldAtlas"]


def qualify(api_key: str, cfg: dict, *, title: str, source: str,
            country: str, buyer: str = "", value: str = "",
            url: str = "") -> dict:
    types = ", ".join(f'"{t}"' for t in cfg["ingest"]["project_types"])
    scopes = ", ".join(f'"{s}"' for s in SCOPES)
    user = (f"Source: {source}\nTitle: {title}\nCountry: {country}\n"
            f"Buyer/entity: {buyer or 'unknown'}\nValue: {value or 'unknown'}\n"
            f"URL: {url or 'n/a'}")
    body = {
        "model": cfg["ingest"]["qualify_model"],
        "max_tokens": 500,
        "system": SYSTEM.replace("{types}", types).replace("{scopes}", scopes),
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

    # Defensive normalisation against the controlled lists.
    if data.get("project_type") not in cfg["ingest"]["project_types"]:
        data["project_type"] = "Other"
    if data.get("fit") not in ("high", "medium", "low"):
        data["fit"] = "medium"
    data["concrete_scope"] = [s for s in (data.get("concrete_scope") or [])
                              if s in SCOPES] or ["Unknown"]
    data["product_fit"] = [p for p in (data.get("product_fit") or [])
                           if p in PRODUCTS]
    return data
