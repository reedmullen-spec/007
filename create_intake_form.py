"""One-off: creates the standalone "007 — Submit a project" intake
database, a form view over it, and a "Pending" table view that hides
already-imported rows. This database is deliberately separate from the
master "007 Projects" database — its own page, its own Notion sharing —
so it can be handed to anyone without ever exposing the master (which
stays restricted to Reed and Issam). Its schema is minimal by design
(Title, Notice URL, Country, Value, Currency, Notes, Imported), so unlike
a form view on the master there's nothing to hide: every field on the
form is meant to be filled in.

Rows aren't deleted once imported (never-delete-rows is a repo-wide
convention — see CLAUDE.md) — the "Pending" view just filters them out
of what a submitter sees day to day; the imported ones remain visible in
the database's default view as an audit trail.

Run any time — each piece (database, form view, pending view) is only
created if missing, cached in state/notion.json, so re-running is a no-op.

Usage:
    python create_intake_form.py
"""
from __future__ import annotations

from src import state
from src.config import env, load_config
from src.notion_client import NotionClient

FORM_VIEW_NAME = "Submit a project"
PENDING_VIEW_NAME = "Pending"


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    intake_db = notion.ensure_intake_database()
    cached = state.load("notion")
    changed = False

    if not cached.get("intake_form_view_id"):
        view = notion.create_view(FORM_VIEW_NAME, "form", database_id=intake_db)
        cached["intake_form_view_id"] = view.get("id", "")
        changed = True
        print(f"Created form view '{FORM_VIEW_NAME}' (id {cached['intake_form_view_id']}).")
    else:
        print(f"Form view already created: {cached['intake_form_view_id']}")

    if not cached.get("intake_pending_view_id"):
        pending = notion.create_view(
            PENDING_VIEW_NAME, "table", database_id=intake_db,
            filter_obj={"property": "Imported", "checkbox": {"equals": False}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}])
        cached["intake_pending_view_id"] = pending.get("id", "")
        changed = True
        print(f"Created '{PENDING_VIEW_NAME}' view (id {cached['intake_pending_view_id']}) "
              "— hides rows already imported into the master.")
    else:
        print(f"'{PENDING_VIEW_NAME}' view already created: {cached['intake_pending_view_id']}")

    if changed:
        state.save("notion", cached)

    print()
    print(f"Intake database: {intake_db}")
    print("Manual step required (Notion has no API for per-person invites):")
    print("  1. Open the '007 — Submit a project' database in Notion.")
    print(f"  2. Share it (or just the '{FORM_VIEW_NAME}' view) with whoever should "
          "submit projects — this page is separate from 007 Projects, so "
          "they never get access to the master database.")
    print("  3. Set the default view submitters land on to "
          f"'{PENDING_VIEW_NAME}' or '{FORM_VIEW_NAME}', not the raw table, "
          "so they don't have to scroll past already-imported rows.")
    print("  4. Confirm the master '007 Projects' database's own sharing is "
          "restricted to you and Issam — that's a separate, unrelated "
          "Notion setting this script does not touch.")
    print("New submissions are pulled into the master automatically by "
          "ingest.py's regular run (twice daily) via src/intake.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
