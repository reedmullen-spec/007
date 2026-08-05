"""Per-AE Notion pages — each person's own editable database.

Notion permissions are page-level, not row-level: a filtered view of the
read-only master database *is* the master, so a person with edit access to
the view can edit any row. Each person instead gets a small "My week —
{Name}" database they fully own.

Sync direction (see sync.py for the nightly pull the other way):
  master -> AE page:  facts (mirrored here), written only at Monday triage.
  AE page -> master:  Status, Next action, Next action date, nightly.
  never synced:       Notes, Outcome, Correction needed.
"""
from __future__ import annotations

from .notion_client import NotionClient

AE_DB_TITLE = "My week — {name}"


def ensure_ae_database(notion: NotionClient, person: str) -> str:
    """Find or create person's 'My week — {Name}' database. Cached in
    state/ae_pages.json keyed by person so repeated runs never duplicate it."""
    return notion._ensure_named_database(
        person, AE_DB_TITLE.format(name=person.title()),
        NotionClient.AE_PAGE_SCHEMA, state_file="ae_pages")


def _master_facts(master_row: dict) -> dict:
    props = master_row.get("properties") or {}

    def rt(name):
        vals = (props.get(name) or {}).get("rich_text", [])
        return vals[0].get("plain_text", "") if vals else ""

    def sel(name):
        return (props.get(name) or {}).get("select", {}).get("name", "")

    title = "".join(t.get("plain_text", "") for t in
                    (props.get("Name") or {}).get("title", []))
    return {
        "title": title,
        "fit": sel("Fit"),
        "gc": rt("General contractor"),
        "location": rt("Location"),
        "concrete_start": rt("Expected concrete start"),
        "value_band": sel("Value band"),
    }


def upsert_ae_row(notion: NotionClient, person: str, master_row: dict,
                  reason: str) -> None:
    """Create or update this project's row on person's page. Only the
    master-owned fact fields (+ Why this project) are written here — the
    AE-owned fields are never touched on update, and only initialised
    (Status = This week) the first time the row is created."""
    db_id = ensure_ae_database(notion, person)
    master_url = master_row.get("url", "")
    facts = _master_facts(master_row)

    props = {
        "Master row": {"url": master_url},
        "Why this project": NotionClient._rt(reason),
        "Fit": {"select": {"name": facts["fit"]}} if facts["fit"] else {"select": None},
        "GC": NotionClient._rt(facts["gc"]),
        "Location": NotionClient._rt(facts["location"]),
        "Expected concrete start": NotionClient._rt(facts["concrete_start"]),
        "Value band": ({"select": {"name": facts["value_band"]}}
                       if facts["value_band"] else {"select": None}),
    }

    existing = notion.query_rows(
        {"property": "Master row", "url": {"equals": master_url}},
        database_id=db_id, limit=1)
    if existing:
        notion.update_properties(existing[0]["id"], props)
        return

    props["Project"] = {"title": [{"text": {"content": facts["title"][:200]}}]}
    props["Status"] = {"select": {"name": "This week"}}
    notion.create_page(db_id, props)
