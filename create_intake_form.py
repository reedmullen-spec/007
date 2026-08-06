"""One-off: creates the standalone "007 — Submit a project" intake
database and a form view over it. This database is deliberately separate
from the master "007 Projects" database — its own page, its own Notion
sharing — so it can be handed to anyone without ever exposing the master
(which stays restricted to Reed and Issam). Its schema is minimal by
design (Title, Notice URL, Country, Value, Currency, Notes), so unlike a
form view on the master there's nothing to hide: every field on the form
is meant to be filled in.

Run once — both the database and the view id are cached in
state/notion.json, so a second run is a no-op.

Usage:
    python create_intake_form.py
"""
from __future__ import annotations

from src import state
from src.config import env, load_config
from src.notion_client import NotionClient

VIEW_NAME = "Submit a project"


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    intake_db = notion.ensure_intake_database()

    cached = state.load("notion")
    view_id = cached.get("intake_form_view_id")
    if view_id:
        print(f"Intake database: {intake_db}")
        print(f"Form view already created: {view_id}")
        return 0

    view = notion.create_view(VIEW_NAME, "form", database_id=intake_db)
    view_id = view.get("id", "")
    cached["intake_form_view_id"] = view_id
    state.save("notion", cached)

    print(f"Created intake database (id {intake_db}) and form view "
          f"'{VIEW_NAME}' (id {view_id}).")
    print()
    print("Manual step required (Notion has no API for per-person invites):")
    print("  1. Open the '007 — Submit a project' database in Notion.")
    print(f"  2. Share it (or just the '{VIEW_NAME}' view) with whoever should "
          "submit projects — this page is separate from 007 Projects, so "
          "they never get access to the master database.")
    print("  3. Confirm the master '007 Projects' database's own sharing is "
          "restricted to you and Issam — that's a separate, unrelated "
          "Notion setting this script does not touch.")
    print("New submissions are pulled into the master automatically by "
          "ingest.py's regular run (twice daily) via src/intake.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
