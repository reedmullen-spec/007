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

    # ------------------------------------------------------- V1 schema
    SCHEMA = {
        "Source": {"select": {"options": [{"name": s} for s in
                   ("TED", "FTS", "AUSTENDER", "SAM", "NEWS", "MANUAL")]}},
        "Region": {"select": {"options": [{"name": s} for s in
                   ("uk", "eu", "us_east", "us_west", "us", "ca", "au")]}},
        "Country": {"rich_text": {}},
        "Location": {"rich_text": {}},
        "Lat": {"number": {}},
        "Lng": {"number": {}},
        "General contractor": {"rich_text": {}},
        "Project type": {"select": {}},
        "Phase": {"select": {"options": [{"name": s} for s in
                  ("Tender", "PCSA / preconstruction", "Starting",
                   "On site", "Finishing", "Unknown")]}},
        "Value": {"number": {}},
        "Currency": {"select": {"options": [{"name": s} for s in
                     ("EUR", "GBP", "USD", "AUD")]}},
        "Expected concrete start": {"rich_text": {}},
        "Fit": {"select": {"options": [
            {"name": "high", "color": "green"},
            {"name": "medium", "color": "yellow"},
            {"name": "low", "color": "gray"}]}},
        "Fit reason": {"rich_text": {}},
        "Status": {"select": {"options": [
            {"name": "New", "color": "gray"},
            {"name": "This week", "color": "blue"},
            {"name": "Working on", "color": "yellow"},
            {"name": "Recontact later", "color": "orange"},
            {"name": "On the project", "color": "green"},
            {"name": "Disqualified", "color": "red"},
            {"name": "Lost", "color": "red"}]}},
        "Recontact date": {"date": {}},
        "Notice URL": {"url": {}},
        "Summary": {"rich_text": {}},
        # ---- extended filterable criteria (Aug 2026) ----
        "Client": {"rich_text": {}},
        "JV / parents": {"rich_text": {}},
        "Concrete subcontractor": {"rich_text": {}},
        "Concrete scope": {"multi_select": {"options": [{"name": s} for s in
            ("In-situ frame", "Mass concrete", "Piling / foundations",
             "Precast", "Tunnel linings", "Marine / water-retaining",
             "DfMA / modular", "Slabs / pavements", "Unknown")]}},
        "Product fit": {"multi_select": {"options": [
            {"name": "Cure / Signal", "color": "blue"},
            {"name": "Data Hub", "color": "purple"},
            {"name": "MixAI", "color": "green"},
            {"name": "FieldAtlas", "color": "orange"}]}},
        "AE": {"select": {"options": [{"name": s} for s in
            ("lisa", "aled", "avi", "alex", "jamie", "jeremy", "justin",
             "lawson", "alicia", "ben", "britain", "brady", "dan",
             "unassigned")]}},
        "Partner route": {"select": {"options": [
            {"name": "Direct"}, {"name": "White Cap"},
            {"name": "Hakron"}, {"name": "Agency"}, {"name": "TBD"}]}},
        "Value band": {"select": {"options": [
            {"name": "<50M", "color": "gray"},
            {"name": "50-250M", "color": "yellow"},
            {"name": "250M+", "color": "red"}]}},
        "Tender deadline": {"date": {}},
        "Announced": {"date": {}},
        "HubSpot deal": {"url": {}},
    }

    def ensure_schema(self) -> None:
        """Add any missing V1 properties to the database (idempotent)."""
        db_id = self.ensure_database()
        db = self._check(self.session.get(f"{BASE}/databases/{db_id}", timeout=30))
        existing = set((db.get("properties") or {}).keys())
        missing = {k: v for k, v in self.SCHEMA.items() if k not in existing}
        # inject project-type options from config
        if "Project type" in missing:
            types = self.cfg.get("_project_types", [])
            missing["Project type"] = {"select": {"options": [{"name": t} for t in types]}}
        if missing:
            self._check(self.session.patch(
                f"{BASE}/databases/{db_id}",
                json={"properties": missing}, timeout=30))
            print(f"Notion schema: added {sorted(missing)}")

    @staticmethod
    def _rt(text: str) -> dict:
        return {"rich_text": [{"text": {"content": (text or "")[:1900]}}]}

    def create_project_row(self, fields: dict) -> dict:
        """Create a fully-populated V1 project row."""
        props = {
            self.cfg["title_property"]: {
                "title": [{"text": {"content": fields["title"][:200]}}]},
            self.cfg["notice_id_property"]: self._rt(fields["notice_id"]),
            "Source": {"select": {"name": fields["source"]}},
            "Region": {"select": {"name": fields["region"]}},
            "Country": self._rt(fields.get("country", "")),
            "Location": self._rt(fields.get("location", "")),
            "General contractor": self._rt(fields.get("gc", "")),
            "Phase": {"select": {"name": fields.get("phase", "Unknown")}},
            "Expected concrete start": self._rt(fields.get("concrete_start", "")),
            "Fit": {"select": {"name": fields.get("fit", "medium")}},
            "Fit reason": self._rt(fields.get("fit_reason", "")),
            "Status": {"select": {"name": "New"}},
            "Summary": self._rt(fields.get("summary", "")),
        }
        if fields.get("project_type"):
            props["Project type"] = {"select": {"name": fields["project_type"]}}
        if fields.get("value") is not None:
            props["Value"] = {"number": fields["value"]}
            props["Currency"] = {"select": {"name": fields.get("currency", "EUR")}}
        if fields.get("lat") is not None:
            props["Lat"] = {"number": fields["lat"]}
            props["Lng"] = {"number": fields["lng"]}
        if fields.get("url"):
            props["Notice URL"] = {"url": fields["url"]}
        # extended criteria
        props["Client"] = self._rt(fields.get("client", ""))
        props["JV / parents"] = self._rt(fields.get("jv_parents", ""))
        props["Concrete subcontractor"] = self._rt(fields.get("subcontractor", ""))
        if fields.get("concrete_scope"):
            props["Concrete scope"] = {"multi_select": [
                {"name": s} for s in fields["concrete_scope"]]}
        if fields.get("product_fit"):
            props["Product fit"] = {"multi_select": [
                {"name": s} for s in fields["product_fit"]]}
        props["AE"] = {"select": {"name": fields.get("ae") or "unassigned"}}
        props["Partner route"] = {"select": {"name": fields.get("partner_route", "TBD")}}
        if fields.get("value") is not None:
            band = ("<50M" if fields["value"] < 50_000_000
                    else "50-250M" if fields["value"] < 250_000_000 else "250M+")
            props["Value band"] = {"select": {"name": band}}
        if fields.get("deadline"):
            props["Tender deadline"] = {"date": {"start": fields["deadline"][:10]}}
        if fields.get("announced"):
            props["Announced"] = {"date": {"start": fields["announced"][:10]}}
        body = {"parent": {"database_id": self.ensure_database()},
                "properties": props}
        return self._check(self.session.post(f"{BASE}/pages", json=body, timeout=30))

    def query_rows(self, filter_obj: dict, sorts: list | None = None,
                   limit: int = 100) -> list[dict]:
        body: dict = {"filter": filter_obj, "page_size": min(limit, 100)}
        if sorts:
            body["sorts"] = sorts
        data = self._check(self.session.post(
            f"{BASE}/databases/{self.ensure_database()}/query",
            json=body, timeout=30))
        return data.get("results", [])

    def row_title(self, row: dict) -> str:
        prop = (row.get("properties") or {}).get(self.cfg["title_property"]) or {}
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))

    def update_properties(self, page_id: str, props: dict) -> None:
        self._check(self.session.patch(f"{BASE}/pages/{page_id}",
                                       json={"properties": props}, timeout=30))

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
