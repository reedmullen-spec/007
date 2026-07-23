"""Local JSON state committed back to the repo between Actions runs.

Two files:
  state/seen.json      -> dedup keys already posted to Slack (tenders + news)
  state/created.json   -> dedup keys already turned into HubSpot deals

The HubSpot `tender_notice_id` pre-check is the real source of truth for
deal dedup; these files just stop Slack re-posts if state and CRM diverge.
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def _path(name: str) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{name}.json"


def load(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save(name: str, data: dict) -> None:
    _path(name).write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
