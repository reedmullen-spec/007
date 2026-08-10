"""Weekly focus — lets each person steer their own Monday triage list.

Reads the "007 Weekly focus" database for this week's entry (if any, entered
before the Sunday-evening cutoff) and, when present, ranks that person's
candidate rows against it with one small Anthropic call. Mirrors
qualify.py's call shape: plain requests.post, JSON-only system prompt, no
web search — this is a ranking pass over data we already have, not research.
"""
from __future__ import annotations

import datetime as _dt
import json

import requests

from .notion_client import NotionClient

API_URL = "https://api.anthropic.com/v1/messages"

RANK_SYSTEM = """You rank construction projects against one person's stated
weekly focus. You are given a focus statement and a numbered list of
candidate projects (id, name, project type, use case, GC, location, value,
fit, stage, expected concrete start). Return ONLY a JSON object, no
markdown, with exactly one key:

picks: array of up to {n} objects, each {{"id": <candidate id>, "reason":
  <one sentence, specific to why this candidate matches the focus>}},
  ordered best match first. If fewer than {n} candidates genuinely match
  the focus, return fewer — never pad the list with weak matches."""


def _week_monday(today: _dt.date | None = None) -> _dt.date:
    today = today or _dt.date.today()
    return today - _dt.timedelta(days=today.weekday())


def get_focus(notion: NotionClient, person: str, cfg: dict,
              now: _dt.datetime | None = None) -> dict | None:
    """This week's focus for `person`, or None if there isn't one, it's for
    the wrong week, arrived after the Sunday deadline, or is blank.
    Returns {"text": str, "row_id": str}."""
    now = now or _dt.datetime.utcnow()
    monday = _week_monday(now.date())
    deadline_hour = cfg["triage"].get("focus_deadline_hour_utc", 20)
    cutoff = _dt.datetime.combine(monday - _dt.timedelta(days=1),
                                  _dt.time(hour=deadline_hour))

    rows = notion.query_rows(
        {"and": [
            {"property": "Person", "select": {"equals": person}},
            {"property": "Week starting", "date": {"equals": monday.isoformat()}},
        ]},
        database_id=notion.ensure_focus_database(),
    )
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("last_edited_time", ""), reverse=True)
    row = rows[0]
    try:
        created = _dt.datetime.fromisoformat(
            row.get("created_time", "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        created = None
    if created is not None and created > cutoff:
        return None

    rt = ((row.get("properties") or {}).get("Focus") or {}).get("rich_text", [])
    text = rt[0].get("plain_text", "") if rt else ""
    if not text.strip():
        return None
    return {"text": text, "row_id": row["id"]}


def _candidate_line(idx: int, row: dict) -> str:
    props = row.get("properties") or {}

    def rt(name):
        vals = (props.get(name) or {}).get("rich_text", [])
        return vals[0].get("plain_text", "") if vals else ""

    def sel(name):
        return ((props.get(name) or {}).get("select") or {}).get("name", "")

    def ms(name):
        return ",".join(o.get("name", "") for o in (props.get(name) or {}).get("multi_select", []))

    title = "".join(t.get("plain_text", "") for t in (props.get("Name") or {}).get("title", []))
    value = (props.get("Value") or {}).get("number")
    return (f"{idx}. {title[:100]} | type={sel('Project type')} | use_case={ms('Use case')} | "
            f"gc={rt('General contractor')} | location={rt('Location')} | value={value} | "
            f"fit={sel('Fit')} | stage={sel('Project stage')} | "
            f"concrete_start={rt('Expected concrete start')}")


def rank_with_focus(api_key: str, cfg: dict, focus_text: str,
                    candidates: list[dict], n: int) -> list[dict]:
    """Ranks `candidates` (Notion project rows) against `focus_text`.
    Returns up to n {"row": <row>, "reason": <str>} dicts, best first —
    fewer if fewer genuinely match (no padding)."""
    if not candidates:
        return []
    capped = candidates[:100]
    by_id = {str(i): row for i, row in enumerate(capped)}
    lines = [_candidate_line(i, row) for i, row in enumerate(capped)]

    body = {
        "model": cfg["ingest"]["qualify_model"],
        "max_tokens": 1200,
        "system": RANK_SYSTEM.replace("{n}", str(n)),
        "messages": [{"role": "user",
                      "content": f"Focus: {focus_text}\n\nCandidates:\n" + "\n".join(lines)}],
    }
    resp = requests.post(API_URL, json=body, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Focus ranking call failed ({resp.status_code}): {resp.text[:300]}")
    text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                   if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)

    picks = []
    for p in (data.get("picks") or [])[:n]:
        row = by_id.get(str(p.get("id")))
        if row is not None:
            picks.append({"row": row, "reason": p.get("reason", "")})
    return picks


def mark_applied(notion: NotionClient, focus_row_id: str) -> None:
    notion.update_properties(focus_row_id, {"Applied": {"checkbox": True}})
