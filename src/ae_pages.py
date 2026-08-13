"""Per-AE Notion pages — each person's own editable database.

Notion permissions are page-level, not row-level: a filtered view of the
read-only master database *is* the master, so a person with edit access to
the view can edit any row. Each person instead gets a small "My week —
{Name}" database they fully own.

Sync direction (see sync.py for the nightly pull the other way):
  master -> AE page:  facts (FACT_FIELDS), mirrored at Monday triage.
  AE page -> master:  Status, Notes, Outcome, Correction needed —
                       nightly, always.
  AE page -> master, fact fields: only when a person edits one of the
                       mirrored facts directly. That edit is treated as
                       authoritative — pushed to the master immediately,
                       and no longer overwritten by later mirrors — see
                       sync_fact_drift() and state/ae_fact_snapshots.json,
                       which records what was last mirrored so a genuine
                       edit can be told apart from "unchanged since we
                       wrote it" (Notion has no per-property edit history,
                       only a page-level last_edited_time).
"""
from __future__ import annotations

from .notion_client import NotionClient

AE_DB_TITLE = "My week — {name}"

# AE-page field -> (master field, _master_facts() dict key, value type).
# These are the facts mirrored master->AE every Monday; if a person edits
# one directly on their own page, it flows back — see sync_fact_drift().
FACT_FIELDS = {
    "Fit": ("Fit", "fit", "select"),
    "GC": ("General contractor/JV", "gc", "rich_text"),
    "Location": ("Location", "location", "rich_text"),
    "Expected concrete start": ("Expected concrete start", "concrete_start", "rich_text"),
    "Expected completion": ("Expected completion", "completion", "date"),
    "Value band": ("Value band", "value_band", "select"),
}


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
        return ((props.get(name) or {}).get("select") or {}).get("name", "")

    def date(name):
        return ((props.get(name) or {}).get("date") or {}).get("start") or ""

    title = "".join(t.get("plain_text", "") for t in
                    (props.get("Name") or {}).get("title", []))
    return {
        "title": title,
        "fit": sel("Fit"),
        "gc": rt("General contractor/JV"),
        "location": rt("Location"),
        "concrete_start": rt("Expected concrete start"),
        "completion": date("Expected completion"),
        "value_band": sel("Value band"),
    }


def _prop_read(props: dict, name: str, kind: str) -> str:
    prop = props.get(name) or {}
    if kind == "select":
        return (prop.get("select") or {}).get("name", "")
    if kind == "date":
        return (prop.get("date") or {}).get("start") or ""
    vals = prop.get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def _prop_write(value: str, kind: str) -> dict:
    if kind == "select":
        return {"select": {"name": value}} if value else {"select": None}
    if kind == "date":
        return {"date": {"start": value}} if value else {"date": None}
    return NotionClient._rt(value)


def sync_fact_drift(notion: NotionClient, ae_row: dict, master_row_id: str) -> set[str]:
    """Compares ae_row's current fact-field values against what was last
    mirrored onto it. Any field a person has since edited gets pushed to
    the master right away and the snapshot updated to match, so it's
    recognised as "no longer drifted" next time — not re-detected, and
    not clobbered by the next mirror. Returns the set of AE-page field
    names found edited this call (so a caller about to re-mirror facts
    onto the same row knows which ones to leave alone)."""
    from . import state

    snapshots = state.load("ae_fact_snapshots")
    row_id = ae_row["id"]
    snap = snapshots.get(row_id, {})
    if not snap:
        return set()   # nothing mirrored yet (or pre-dates this feature) — no baseline to diff against
    ae_props = ae_row.get("properties") or {}

    pushback = {}
    edited = set()
    for ae_field, (master_field, _fact_key, kind) in FACT_FIELDS.items():
        if ae_field not in snap:
            continue
        current = _prop_read(ae_props, ae_field, kind)
        if current != snap[ae_field]:
            pushback[master_field] = _prop_write(current, kind)
            snap[ae_field] = current
            edited.add(ae_field)

    if pushback:
        notion.update_properties(master_row_id, pushback)
        snapshots[row_id] = snap
        state.save("ae_fact_snapshots", snapshots)
    return edited


def upsert_ae_row(notion: NotionClient, person: str, master_row: dict,
                  reason: str) -> str:
    """Create or update this project's row on person's page. Facts are
    re-mirrored from the master UNLESS the person has edited that specific
    field since the last mirror (see sync_fact_drift) — their edit wins
    and is left alone here (it was already pushed to the master). AE-owned
    fields (Status/Next action/Next action date/Notes/Outcome/Correction
    needed) are never touched on update, and only initialised
    (Status = This week) the first time the row is created.

    Returns this AE-page row's own Notion URL, so triage.py's Slack line
    can link straight to a person's editable page alongside the master's
    (read-only) one."""
    from . import state

    db_id = ensure_ae_database(notion, person)
    master_url = master_row.get("url", "")
    facts = _master_facts(master_row)

    existing = notion.query_rows(
        {"property": "Master row", "url": {"equals": master_url}},
        database_id=db_id, limit=1)
    row_id = existing[0]["id"] if existing else None
    edited = sync_fact_drift(notion, existing[0], master_row["id"]) if existing else set()

    snapshots = state.load("ae_fact_snapshots")
    snap = snapshots.get(row_id, {}) if row_id else {}

    props = {"Master row": {"url": master_url}, "Why this project": NotionClient._rt(reason)}
    for ae_field, (_master_field, fact_key, kind) in FACT_FIELDS.items():
        if ae_field in edited:
            continue   # already pushed to master + snapshotted by sync_fact_drift
        fresh_value = facts[fact_key]
        props[ae_field] = _prop_write(fresh_value, kind)
        snap[ae_field] = fresh_value

    if row_id:
        notion.update_properties(row_id, props)
        snapshots[row_id] = snap
        state.save("ae_fact_snapshots", snapshots)
        return existing[0].get("url", "")

    props["Project"] = {"title": [{"text": {"content": facts["title"][:200]}}]}
    props["Status"] = {"select": {"name": "This week"}}
    created = notion.create_page(db_id, props)
    snapshots[created["id"]] = snap
    state.save("ae_fact_snapshots", snapshots)
    return created.get("url", "")
