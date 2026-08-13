"""007 self-serve actions — Notion checkboxes, no Slack approval needed.

ingest.py rows only ever surface as a plain link in the weekly triage list;
there was no way to ask for enrichment/a deal/contacts on one without going
through the legacy digest.py/news.py card flow. This gives every row three
independent checkboxes instead:

    Enrich          -> deep research pack (same engine as enrich.py).
                       Creates the HubSpot deal first if one doesn't exist
                       yet — the pack needs somewhere to pin its note.
                       Named '[GC] — [Title] — [Location]' (src/deal_naming.py),
                       GC/Location segments dropped when not yet known.
    Create deal     -> HubSpot deal at Identified, same naming convention.
                       Backfills the enrichment TL;DR onto it if Enrich
                       already ran.
    Build contacts  -> Amplemarket buying group (needs General contractor
                       filled in, AND a HubSpot deal already existing —
                       tick Enrich or Create deal first). Belgium/Hakron
                       rows are skipped by design, same rule as
                       contacts.py. Every matched person is also pushed
                       into HubSpot as a contact, associated to that deal.

Order-agnostic in practice: tick any combination, any time. A box that
succeeds is unchecked so it doesn't re-fire; a box that fails is left
checked so the next run retries it. Ticking Enrich and Create deal in the
same run is safe — Enrich runs first, creates the deal if needed, and
Create deal then just finds it already exists (HubSpot dedup, rule #2).
Build contacts needing a deal is likewise never a problem within a single
run: ACTIONS' fixed order (Enrich, Create deal, Build contacts) always
processes it last, so a deal created by either of the other two boxes on
the SAME row in the SAME run already exists by the time Build contacts
fires. It only fails — leaving the box checked for the next run — when
ticked alone with no deal yet at all.

Usage:
    python actions.py               # live
    python actions.py --dry-run     # print what would run, change nothing
"""
from __future__ import annotations

import argparse
import sys

from contacts import build_buying_group
from enrich import enrich_deal
from src.config import env, load_config
from src.deal_naming import build_deal_name
from src.hubspot_client import HubSpotClient
from src.notion_client import NotionClient

TLDR_PROPERTY = "Enrichment summary"


def _prop_checkbox(row: dict, name: str) -> bool:
    return bool(((row.get("properties") or {}).get(name) or {}).get("checkbox"))


def _prop_rich(row: dict, name: str) -> str:
    rich = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return rich[0].get("plain_text", "") if rich else ""


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def _prop_url(row: dict, name: str) -> str:
    return ((row.get("properties") or {}).get(name) or {}).get("url") or ""


def _run_enrich(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    notice_id = _prop_rich(row, notion.cfg["notice_id_property"])
    country = _prop_rich(row, "Country")
    notice_url = _prop_url(row, "Notice URL")
    ae = _prop_select(row, "AE")
    if not ae or ae == "unassigned":
        ae = "aled"

    existing = hubspot.find_deal_by_notice_id(notice_id)
    if existing:
        deal_id = existing["id"]
        deal_name = existing.get("properties", {}).get("dealname") or title
    else:
        gc = _prop_rich(row, "General contractor")
        location = _prop_rich(row, "Location")
        deal_name = build_deal_name(title, contractor=gc, location=location)
        deal = hubspot.create_deal(name=deal_name, notice_id=notice_id, ae=ae)
        deal_id = deal["id"]
        print(f"Created deal {deal_id} to carry the research pack: {deal['portal_url']}")

    enrich_deal(cfg, hubspot, deal_id=deal_id, deal_name=deal_name, notice_id=notice_id,
               ae=ae, country=country, notice_url=notice_url, slack=None)


def _run_create_deal(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    notice_id = _prop_rich(row, notion.cfg["notice_id_property"])
    ae = _prop_select(row, "AE") or "unassigned"
    gc = _prop_rich(row, "General contractor")
    location = _prop_rich(row, "Location")
    deal_name = build_deal_name(title, contractor=gc, location=location)

    existing = hubspot.find_deal_by_notice_id(notice_id)
    if existing:
        deal_id = existing["id"]
        print(f"Deal already exists ({deal_id}) for '{title[:60]}' — no new deal created.")
    else:
        tldr = _prop_rich(row, TLDR_PROPERTY)   # backfilled if Enrich already ran
        deal = hubspot.create_deal(name=deal_name, notice_id=notice_id, ae=ae,
                                   summary=tldr or None)
        deal_id = deal["id"]
        print(f"Created deal {deal_id}: {deal['portal_url']}")

    notion.update_properties(row["id"], {
        "HubSpot deal": {"url": f"https://app.hubspot.com/contacts/"
                                f"{cfg['hubspot']['portal_id']}/deal/{deal_id}"}})


def _run_contacts(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    gc = _prop_rich(row, "General contractor")
    country = _prop_rich(row, "Country")
    ae = _prop_select(row, "AE") or "aled"
    if not gc:
        raise RuntimeError("General contractor is blank — fill it in before requesting contacts.")

    notice_id = _prop_rich(row, notion.cfg["notice_id_property"])
    deal = hubspot.find_deal_by_notice_id(notice_id)
    if not deal:
        raise RuntimeError("No HubSpot deal exists yet for this project — "
                           "tick Enrich or Create deal first.")

    skip_countries = [c.upper() for c in cfg.get("hakron_skip_contacts_countries", [])]
    if country.upper() in skip_countries:
        print(f"{country}: Hakron partner path — contact build skipped by design "
             f"(the research pack goes to Hakron, not a contact list).")
        return

    framework = cfg["enrichment"]["framework_by_ae"].get(ae, "concretedna")
    result = build_buying_group(cfg, company=gc, project=title, framework=framework,
                                country=country, hubspot=hubspot, deal_id=deal["id"])
    link = result.get("url") or ""
    if link.startswith("http"):
        try:
            notion.update_properties(row["id"], {"Contacts list": {"url": link}})
        except Exception:
            pass


ACTIONS = [
    ("Enrich", _run_enrich),
    ("Create deal", _run_create_deal),
    ("Build contacts", _run_contacts),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)
    if not args.dry_run:
        # Runs every 20 minutes, independent of ingest.py's 02:00/13:00
        # UTC schedule — a schema property added in the same PR as this
        # script (e.g. the Enrich/Create deal/Build contacts checkboxes)
        # must not have to wait for ingest.py to happen to run first.
        notion.ensure_schema()
        hubspot.ensure_notice_property()
        hubspot.ensure_summary_property()

    rows = notion.query_all_rows({"or": [
        {"property": prop, "checkbox": {"equals": True}} for prop, _ in ACTIONS
    ]})
    if not rows:
        print("No pending actions.")
        return 0

    done = 0
    for row in rows:
        title = notion.row_title(row)[:70]
        for prop, fn in ACTIONS:
            if not _prop_checkbox(row, prop):
                continue
            if args.dry_run:
                print(f"[dry-run] would run {prop!r} for '{title}'")
                continue
            try:
                fn(cfg, notion, hubspot, row)
                notion.update_properties(row["id"], {prop: {"checkbox": False}})
                done += 1
                print(f"{prop} done for '{title}'")
            except Exception as exc:
                print(f"WARNING: {prop} failed for '{title}': {exc}", file=sys.stderr)

    print(f"Processed {done} action(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
