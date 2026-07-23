"""Notion client — find-or-create a row in the shared Projects database,
keyed on the tender notice ID, and write the research pack into the row body.

Requires: internal integration secret (NOTION_TOKEN) AND the database being
connected to the integration (page -> ... -> Connections).
"""
from __future__ import annotations

import requests

BASE = "https://api.notion.com/v1"
VERSION = "2022-06-28"
MAX_BLOCK_CHARS = 1900  # Notion caps rich_text at 2000 chars per block


class NotionClient:
    def __init__(self, token: str, cfg: dict):
        self.cfg = cfg["notion"]
        self.database_id: str | None = self.cfg.get("database_id") or None
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def ensure_database(self) -> str:
        """Find or create the '007 Projects' database inside the parent page.
        The resolved ID is cached in state/notion.json between runs."""
        from . import state

        if self.database_id:
            return self.database_id
        cached = state.load("notion")
        if cached.get("database_id"):
            self.database_id = cached["database_id"]
            return self.database_id

        parent = self.cfg["parent_page_id"]
        wanted = self.cfg.get("database_name", "007 Projects")

        # Reuse an existing child database with the right name, if present.
        data = self._check(self.session.get(
            f"{BASE}/blocks/{parent}/children", params={"page_size": 100},
            timeout=30))
        for block in data.get("results", []):
            if block.get("type") == "child_database" and \
                    block.get("child_database", {}).get("title") == wanted:
                self.database_id = block["id"]
                break

        if not self.database_id:
            body = {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": wanted}}],
                "properties": {
                    self.cfg["title_property"]: {"title": {}},
                    self.cfg["notice_id_property"]: {"rich_text": {}},
                },
            }
            created = self._check(self.session.post(
                f"{BASE}/databases", json=body, timeout=30))
            self.database_id = created["id"]
            print(f"Created Notion database '{wanted}': {created.get('url', '')}")

        state.save("notion", {"database_id": self.database_id})
        return self.database_id

    def _check(self, resp: requests.Response) -> dict:
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Notion API failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def find_row(self, notice_id: str) -> dict | None:
        body = {
            "filter": {
                "property": self.cfg["notice_id_property"],
                "rich_text": {"equals": notice_id},
            },
            "page_size": 1,
        }
        data = self._check(self.session.post(
            f"{BASE}/databases/{self.ensure_database()}/query", json=body, timeout=30))
        results = data.get("results", [])
        return results[0] if results else None

    def create_row(self, title: str, notice_id: str) -> dict:
        body = {
            "parent": {"database_id": self.ensure_database()},
            "properties": {
                self.cfg["title_property"]: {
                    "title": [{"text": {"content": title[:200]}}]},
                self.cfg["notice_id_property"]: {
                    "rich_text": [{"text": {"content": notice_id}}]},
            },
        }
        return self._check(self.session.post(f"{BASE}/pages", json=body, timeout=30))

    def find_or_create_row(self, title: str, notice_id: str) -> dict:
        return self.find_row(notice_id) or self.create_row(title, notice_id)

    def append_pack(self, page_id: str, markdown: str) -> None:
        """Write the pack as blocks (headings + paragraphs, chunked)."""
        blocks = list(_markdown_to_blocks(markdown))
        # Notion accepts max 100 children per append call.
        for i in range(0, len(blocks), 100):
            self._check(self.session.patch(
                f"{BASE}/blocks/{page_id}/children",
                json={"children": blocks[i:i + 100]}, timeout=60))


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:MAX_BLOCK_CHARS]}}]


def _markdown_to_blocks(md: str):
    """Minimal markdown -> Notion blocks: #/##/### headings, bullets, paras."""
    for raw_line in md.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("### "):
            yield {"type": "heading_3",
                   "heading_3": {"rich_text": _rich(stripped[4:])}}
        elif stripped.startswith("## "):
            yield {"type": "heading_2",
                   "heading_2": {"rich_text": _rich(stripped[3:])}}
        elif stripped.startswith("# "):
            yield {"type": "heading_1",
                   "heading_1": {"rich_text": _rich(stripped[2:])}}
        elif stripped.startswith(("- ", "* ")):
            yield {"type": "bulleted_list_item",
                   "bulleted_list_item": {"rich_text": _rich(stripped[2:])}}
        else:
            # chunk long paragraphs to respect the per-block limit
            for i in range(0, len(stripped), MAX_BLOCK_CHARS):
                yield {"type": "paragraph",
                       "paragraph": {"rich_text": _rich(stripped[i:i + MAX_BLOCK_CHARS])}}
