"""One-off setup: creates an "Active projects" view on the master database
that hides Status=Disqualified rows entirely, so they don't crowd out real
projects when someone opens the database directly in Notion (the automated
paths — triage, AE pages — already exclude Disqualified; this is purely
for the raw database view a human scans).

Not idempotent — rerunning creates a second view with the same name. It's
a one-time setup action; if you need it again, delete the duplicate view
in Notion's UI rather than scripting dedup logic for something run once.

Usage:
    python create_active_view.py
"""
from __future__ import annotations

from src.config import env, load_config
from src.notion_client import NotionClient


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    created = notion.create_view(
        "Active projects", "table",
        filter_obj={"property": "Status", "select": {"does_not_equal": "Disqualified"}},
        sorts=[{"property": "Announced", "direction": "descending"}],
    )
    print(f"Created view 'Active projects': {created.get('url', created.get('id'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
