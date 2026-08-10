"""Notion client — find-or-create a row in the shared Projects database,
keyed on the tender notice ID, and write the research pack into the row body.

Requires: internal integration secret (NOTION_TOKEN) AND the database being
connected to the integration (page -> ... -> Connections).
"""
from __future__ import annotations

import requests

BASE = "https://api.notion.com/v1"
VERSION = "2022-06-28"
# The Views API (create/update database views) needs a newer version than
# everything else this client does. Kept as a per-request override rather
# than bumping VERSION globally — a version bump can silently change
# response shapes for calls the whole pipeline already depends on.
VIEWS_VERSION = "2026-03-11"
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

    def _ensure_named_database(self, cache_key: str, title: str,
                               properties: dict, state_file: str = "notion") -> str:
        """Find-or-create a child database with `title` under the shared
        parent page (MI6 teamspace), caching its id in
        state/{state_file}.json under `cache_key` between runs.

        Also patches in any properties missing from an already-created
        database (ADD-only, same idempotent rule as ensure_schema()) —
        schemas like AE_PAGE_SCHEMA have grown fields (e.g. Expected
        completion) after some people's pages already existed; a cached
        id alone would silently skip ever picking those up, which is
        exactly what crashed triage.py writing to an AE page created
        before that field existed."""
        from . import state

        cached = state.load(state_file)
        db_id = cached.get(cache_key)

        if not db_id:
            parent = self.cfg["parent_page_id"]
            # Reuse an existing child database with the right name, if present.
            data = self._check(self.session.get(
                f"{BASE}/blocks/{parent}/children", params={"page_size": 100},
                timeout=30))
            for block in data.get("results", []):
                if block.get("type") == "child_database" and \
                        block.get("child_database", {}).get("title") == title:
                    db_id = block["id"]
                    break

        if not db_id:
            body = {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties,
            }
            created = self._check(self.session.post(
                f"{BASE}/databases", json=body, timeout=30))
            db_id = created["id"]
            print(f"Created Notion database '{title}': {created.get('url', '')} "
                  f"— share it with the right person(s), the API can't do that part.")
        else:
            db = self._check(self.session.get(f"{BASE}/databases/{db_id}", timeout=30))
            existing = set((db.get("properties") or {}).keys())
            missing = {k: v for k, v in properties.items() if k not in existing}
            if missing:
                self._check(self.session.patch(
                    f"{BASE}/databases/{db_id}",
                    json={"properties": missing}, timeout=30))
                print(f"'{title}' schema: added {sorted(missing)}")

        cached[cache_key] = db_id
        state.save(state_file, cached)
        return db_id

    def ensure_database(self) -> str:
        """Find or create the '007 Projects' database inside the parent page.
        The resolved ID is cached in state/notion.json between runs."""
        if self.database_id:
            return self.database_id
        wanted = self.cfg.get("database_name", "007 Projects")
        self.database_id = self._ensure_named_database(
            "database_id", wanted,
            {
                self.cfg["title_property"]: {"title": {}},
                self.cfg["notice_id_property"]: {"rich_text": {}},
            },
        )
        return self.database_id

    def _check(self, resp: requests.Response) -> dict:
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Notion API failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    # ------------------------------------------------------- V1 schema
    # Mirrors the live "007 Projects" schema exactly (verified Aug 2026).
    # Option strings are compared by equality against qualify output — do not
    # reword them. NOTE: "Awarded - pre-start" uses a plain hyphen.
    PROJECT_TYPES = [
        "Bridge / viaduct", "Tunnel", "Highway / road", "Rail / metro",
        "Airport", "Port / marine", "Water treatment", "Wastewater treatment",
        "Dam / hydro / reservoir", "Flood & coastal defence",
        "Hospital / healthcare", "Education", "Residential",
        "Student accommodation", "Commercial / office",
        "Retail / hotel / leisure", "Stadium / arena / convention",
        "Data centre", "Nuclear", "Power & transmission",
        "Energy from waste", "Industrial / manufacturing",
        "Life sciences / R&D", "Logistics / warehouse",
        "Precast yard / batching plant", "Prison / justice / defence", "Other",
    ]
    WORK_NATURES = [
        "New build", "Extension", "Replacement", "Widening / twinning",
        "Refurbishment / retrofit", "Phase of larger scheme", "Unknown",
    ]
    USE_CASES = [
        "In-situ frame", "Slipform core", "Raft / substructure / basement",
        "Piling / foundations", "Mass concrete", "Post-tensioned slabs",
        "Precast", "Tunnel linings", "Marine / water-retaining",
        "Culverts / ancillary structures", "Slabs / pavements",
        "DfMA / modular", "Unknown",
    ]
    PRODUCTS = ["Cure / Signal", "Helix", "Data Hub", "MixAI", "FieldAtlas"]
    PROJECT_STAGES = [
        "Planning / pre-tender", "Tender", "PCSA / preconstruction",
        "Awarded - pre-start", "Groundbreaking / enabling works",
        "Main works / on site", "Finishing", "Complete", "Unknown",
    ]
    # "Active Contact" — renamed live in Notion from "Working on" at some
    # point without the code being updated; ensure_schema() only ADDS
    # missing properties, it never renames existing options, so this must
    # stay in sync with Notion by hand.
    STATUSES = [
        ("New", "gray"), ("This week", "blue"), ("Active Contact", "yellow"),
        ("Recontact later", "orange"), ("On the project", "green"),
        ("Disqualified", "red"), ("Lost", "red"),
    ]
    AES = ["lisa", "aled", "avi", "alex", "jamie", "jeremy", "justin",
           "lawson", "alicia", "ben", "britain", "brady", "dan", "unassigned"]

    # Derived from AE via routing.ae_sdr_map (config.yaml), written once at
    # ingest time — see ingest.py. Left blank for AE=unassigned/dan.
    SDRS = ["alex", "jamie", "reed"]

    # Fit is computed deterministically by src/scoring.py — see its docstring
    # for why (reproducibility: same project in, same band out).
    FIT_BANDS = ["High", "Medium", "Low", "Disqualified"]
    FIT_PROFILES = ["Mass-concrete civils", "Schedule-critical vertical build",
                    "Industrialised construction / DfMA", "Low-carbon spec-in",
                    "Precast & batching supply"]

    FOCUS_SCHEMA = {
        "Name": {"title": {}},
        "Person": {"select": {"options": [{"name": a} for a in AES if a != "unassigned"]}},
        "Week starting": {"date": {}},
        "Focus": {"rich_text": {}},
        "Applied": {"checkbox": {}},
    }

    def ensure_focus_database(self) -> str:
        """Find or create the '007 Weekly focus' database, one row per
        person per week. Editable by everyone (shared manually, once)."""
        return self._ensure_named_database(
            "focus_database_id", "007 Weekly focus", self.FOCUS_SCHEMA)

    # Intake: a deliberately tiny, separate database that anyone can be
    # given edit access to without ever touching the master "007 Projects"
    # database (which stays restricted to Reed + Issam). ingest.py sweeps
    # unimported rows through the same qualify/score/route pipeline as
    # manual master rows — see src/intake.py.
    INTAKE_SCHEMA = {
        "Title": {"title": {}},
        "Notice URL": {"url": {}},
        "Country": {"rich_text": {}},
        "Value": {"number": {}},
        "Currency": {"select": {"options": [
            {"name": "EUR"}, {"name": "GBP"}, {"name": "AUD"}, {"name": "USD"}]}},
        "Notes": {"rich_text": {}},
        "Imported": {"checkbox": {}},
    }

    def ensure_intake_database(self) -> str:
        """Find or create the '007 — Submit a project' database — its own
        page, its own sharing, no fields to hide (unlike a form view on
        the master, which inherits every master field)."""
        return self._ensure_named_database(
            "intake_database_id", "007 — Submit a project", self.INTAKE_SCHEMA)

    # Per-AE page: master owns everything except the AE-owned block below
    # (Status / Next action / Next action date / Notes / Outcome /
    # Correction needed) — see src/ae_pages.py and sync.py.
    AE_PAGE_SCHEMA = {
        "Project": {"title": {}},
        "Master row": {"url": {}},
        "Why this project": {"rich_text": {}},
        "Fit": {"select": {"options": [
            {"name": "High", "color": "green"},
            {"name": "Medium", "color": "yellow"},
            {"name": "Low", "color": "gray"},
            {"name": "Disqualified", "color": "red"}]}},
        "GC": {"rich_text": {}},
        "Location": {"rich_text": {}},
        "Expected concrete start": {"rich_text": {}},
        "Expected completion": {"date": {}},
        "Value band": {"select": {"options": [
            {"name": "Under 50M", "color": "gray"},
            {"name": "50-250M", "color": "yellow"},
            {"name": "250M+", "color": "red"}]}},
        "Status": {"select": {"options": [{"name": s, "color": c}
                                          for s, c in STATUSES]}},
        "Next action": {"rich_text": {}},
        "Next action date": {"date": {}},
        "Notes": {"rich_text": {}},
        "Outcome": {"select": {"options": [{"name": s} for s in
                    ("Meeting booked", "No interest", "Wrong contact",
                     "Too early", "Already covered", "Other")]}},
        "Correction needed": {"rich_text": {}},
    }

    def create_page(self, database_id: str, properties: dict) -> dict:
        """Generic page create for any database (per-AE pages, focus rows)."""
        body = {"parent": {"database_id": database_id}, "properties": properties}
        return self._check(self.session.post(f"{BASE}/pages", json=body, timeout=30))

    SCHEMA = {
        "Source": {"select": {"options": [{"name": s} for s in
                   ("TED", "FTS", "AUSTENDER", "SAM", "NEWS", "MANUAL")]}},
        "Summary": {"rich_text": {}},
        "Location": {"rich_text": {}},
        "Country": {"rich_text": {}},
        "Region": {"select": {"options": [{"name": s} for s in
                   ("uk", "eu", "us_east", "us_west", "us", "ca", "au")]}},
        "Lat": {"number": {}},
        "Lng": {"number": {}},
        "General contractor": {"rich_text": {}},
        "JV / parents": {"rich_text": {}},
        "Client": {"rich_text": {}},
        "Concrete subcontractor": {"rich_text": {}},
        "Value": {"number": {}},
        "Currency": {"select": {"options": [{"name": s} for s in
                     ("EUR", "GBP", "USD", "AUD")]}},
        "Value band": {"select": {"options": [
            {"name": "Under 50M", "color": "gray"},
            {"name": "50-250M", "color": "yellow"},
            {"name": "250M+", "color": "red"}]}},
        "Concrete opportunity": {"select": {"options": [{"name": s} for s in
                                 ("Small", "Medium", "Large", "Unknown")]}},
        "Tender deadline": {"date": {}},
        "Announced": {"date": {}},
        "Expected concrete start": {"rich_text": {}},
        "Expected completion": {"date": {}},
        "Project type": {"select": {"options": [{"name": t} for t in PROJECT_TYPES]}},
        "Work nature": {"select": {"options": [{"name": w} for w in WORK_NATURES]}},
        "Use case": {"multi_select": {"options": [{"name": u} for u in USE_CASES]}},
        "Product fit": {"multi_select": {"options": [{"name": p} for p in PRODUCTS]}},
        "Fit": {"select": {"options": [
            {"name": "High", "color": "green"},
            {"name": "Medium", "color": "yellow"},
            {"name": "Low", "color": "gray"},
            {"name": "Disqualified", "color": "red"}]}},
        "Fit profile": {"select": {"options": [{"name": p} for p in FIT_PROFILES]}},
        "Fit reason": {"rich_text": {}},
        "Fit dimensions": {"rich_text": {}},
        "Competitor present": {"rich_text": {}},
        "Verified": {"checkbox": {}},
        "Status": {"select": {"options": [{"name": s, "color": c}
                                          for s, c in STATUSES]}},
        "Project stage": {"select": {"options": [{"name": s} for s in PROJECT_STAGES]}},
        "AE": {"select": {"options": [{"name": a} for a in AES]}},
        "SDR": {"select": {"options": [{"name": s} for s in SDRS]}},
        "Partner route": {"select": {"options": [{"name": s} for s in
                          ("Direct", "White Cap", "Hakron", "Agency", "TBD")]}},
        "Partner contact": {"rich_text": {}},
        "Next action": {"rich_text": {}},
        "Next action date": {"date": {}},
        "Recontact date": {"date": {}},
        "Notice URL": {"url": {}},
        "HubSpot deal": {"url": {}},
        # AE-owned, synced up from their own page nightly (sync.py) — see
        # src/ae_pages.py's FACT_FIELDS for the other sync direction.
        "Notes": {"rich_text": {}},
        "Outcome": {"select": {"options": [{"name": s} for s in
                    ("Meeting booked", "No interest", "Wrong contact",
                     "Too early", "Already covered", "Other")]}},
        "Correction needed": {"rich_text": {}},
    }

    def ensure_schema(self) -> None:
        """Add any missing V1 properties to the database (idempotent)."""
        db_id = self.ensure_database()
        db = self._check(self.session.get(f"{BASE}/databases/{db_id}", timeout=30))
        existing = set((db.get("properties") or {}).keys())
        missing = {k: v for k, v in self.SCHEMA.items() if k not in existing}
        if missing:
            self._check(self.session.patch(
                f"{BASE}/databases/{db_id}",
                json={"properties": missing}, timeout=30))
            print(f"Notion schema: added {sorted(missing)}")

    def get_data_source_id(self, database_id: str | None = None) -> str:
        """The Views API addresses a database's *data source*, not the
        database itself — a newer concept the older API version this
        client otherwise uses doesn't return. Needs the newer version
        header just for this one call."""
        db_id = database_id or self.ensure_database()
        headers = {**self.session.headers, "Notion-Version": VIEWS_VERSION}
        resp = self.session.get(f"{BASE}/databases/{db_id}", headers=headers, timeout=30)
        data = self._check(resp)
        sources = data.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"Database {db_id} has no data_sources — "
                               f"can't create a view on it")
        return sources[0]["id"]

    def create_view(self, name: str, view_type: str, database_id: str | None = None,
                    filter_obj: dict | None = None, sorts: list | None = None) -> dict:
        """Create a database view (Views API, Notion-Version 2026-03-11) —
        an isolated request with its own version header, not the client's
        global VERSION (see the VIEWS_VERSION comment above)."""
        db_id = database_id or self.ensure_database()
        data_source_id = self.get_data_source_id(db_id)
        headers = {**self.session.headers, "Notion-Version": VIEWS_VERSION}
        body: dict = {"database_id": db_id, "data_source_id": data_source_id,
                      "name": name, "type": view_type}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        resp = self.session.post(f"{BASE}/views", json=body, headers=headers, timeout=30)
        return self._check(resp)

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
            "Project stage": {"select": {"name": fields.get("stage", "Unknown")}},
            "Work nature": {"select": {"name": fields.get("work_nature", "Unknown")}},
            "Concrete opportunity": {"select": {"name": fields.get("concrete_opportunity", "Unknown")}},
            "Competitor present": self._rt(fields.get("competitor", "")),
            "Verified": {"checkbox": False},
            "Expected concrete start": self._rt(fields.get("concrete_start", "")),
            "Fit": {"select": {"name": fields.get("fit", "Medium")}},
            "Fit reason": self._rt(fields.get("fit_reason", "")),
            "Fit dimensions": self._rt(fields.get("fit_dimensions", "")),
            "Status": {"select": {"name": "Disqualified"
                                  if fields.get("fit") == "Disqualified" else "New"}},
            "Summary": self._rt(fields.get("summary", "")),
        }
        if fields.get("fit_profile"):
            props["Fit profile"] = {"select": {"name": fields["fit_profile"]}}
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
        if fields.get("use_case"):
            props["Use case"] = {"multi_select": [
                {"name": s} for s in fields["use_case"]]}
        if fields.get("product_fit"):
            props["Product fit"] = {"multi_select": [
                {"name": s} for s in fields["product_fit"]]}
        props["AE"] = {"select": {"name": fields.get("ae") or "unassigned"}}
        if fields.get("sdr"):
            props["SDR"] = {"select": {"name": fields["sdr"]}}
        props["Partner route"] = {"select": {"name": fields.get("partner_route", "TBD")}}
        if fields.get("value") is not None:
            band = ("Under 50M" if fields["value"] < 50_000_000
                    else "50-250M" if fields["value"] < 250_000_000 else "250M+")
            props["Value band"] = {"select": {"name": band}}
        if fields.get("deadline"):
            props["Tender deadline"] = {"date": {"start": fields["deadline"][:10]}}
        if fields.get("announced"):
            props["Announced"] = {"date": {"start": fields["announced"][:10]}}
        if fields.get("completion_date"):
            props["Expected completion"] = {"date": {"start": fields["completion_date"]}}
        body = {"parent": {"database_id": self.ensure_database()},
                "properties": props}
        return self._check(self.session.post(f"{BASE}/pages", json=body, timeout=30))

    def query_rows(self, filter_obj: dict, sorts: list | None = None,
                   limit: int = 100, database_id: str | None = None) -> list[dict]:
        db = database_id or self.ensure_database()
        body: dict = {"filter": filter_obj, "page_size": min(limit, 100)}
        if sorts:
            body["sorts"] = sorts
        data = self._check(self.session.post(
            f"{BASE}/databases/{db}/query",
            json=body, timeout=30))
        return data.get("results", [])

    def query_all_rows(self, filter_obj: dict, sorts: list | None = None,
                       database_id: str | None = None) -> list[dict]:
        """Like query_rows but follows pagination to return every match —
        query_rows caps at one page (100 rows)."""
        db = database_id or self.ensure_database()
        results: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if filter_obj:
                body["filter"] = filter_obj
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            data = self._check(self.session.post(
                f"{BASE}/databases/{db}/query", json=body, timeout=30))
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

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
