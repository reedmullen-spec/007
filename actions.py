"""007 self-serve actions — Notion checkboxes, no Slack approval needed.

ingest.py rows only ever surface as a plain link in the weekly triage list;
there was no way to ask for enrichment/a deal/contacts on one without going
through the legacy digest.py/news.py card flow. This gives every row four
independent checkboxes instead:

    Enrich          -> deep research pack (same engine as enrich.py).
                       Creates the HubSpot deal first if one doesn't exist
                       yet — the pack needs somewhere to pin its note.
                       Named '[GC] — [Title] — [Location]' (src/deal_naming.py),
                       GC/Location segments dropped when not yet known.
    Create deal     -> HubSpot deal at Identified, same naming convention.
                       Backfills the enrichment TL;DR onto it if Enrich
                       already ran, and posts the row's summary onto the
                       deal as a pinned note (the TL;DR when there is one,
                       otherwise the ingest notice summary) so the text is
                       readable in the timeline, not just in a property.
    Create lead     -> HubSpot lead on the general contractor's company
                       record, same naming convention as the deal. Needs
                       General contractor/JV filled in — HubSpot refuses a
                       lead with no company or contact to hang off, so
                       there is nothing sensible to create without it. The
                       company is found by name or created. Independent of
                       Create deal: neither needs the other, and a project
                       can have both.
    Build contacts  -> Amplemarket buying group (needs General contractor/JV
                       filled in, AND a HubSpot deal already existing —
                       tick Enrich or Create deal first). Belgium/Hakron
                       rows are skipped by design, same rule as
                       contacts.py. Every matched person is also pushed
                       into HubSpot as a contact, associated to that deal.
                       A no-op if Contacts list is already set — re-ticking
                       does NOT rebuild (build_buying_group() has no dedup
                       of its own; clear Contacts list first to force a
                       genuine rebuild).

Create lead is the one action needing scopes beyond the shared set
(crm.objects.leads.*, crm.schemas.leads.write, crm.objects.companies.write).
Its lead-property bootstrap runs inside the action rather than in main()'s
preamble alongside the deal ones, deliberately: a portal missing the leads
scopes must fail Create lead only, not take Enrich/Create deal/Build
contacts down with it. check_hubspot_scopes.py probes all of them.

Order-agnostic in practice: tick any combination, any time. A box that
succeeds is unchecked so it doesn't re-fire; a box that fails is left
checked so the next run retries it. Ticking Enrich and Create deal in the
same run is safe — Enrich runs first, creates the deal if needed, and
Create deal then just finds it already exists (HubSpot dedup, rule #2).
Build contacts needing a deal is likewise never a problem within a single
run: ACTIONS' fixed order (Enrich, Create deal, Create lead, Build
contacts) always processes it last, so a deal created by either of the
deal-making boxes on the SAME row in the SAME run already exists by the
time Build contacts fires. It only fails — leaving the box checked for the
next run — when ticked alone with no deal yet at all.

Usage:
    python actions.py               # live
    python actions.py --dry-run     # print what would run, change nothing
"""
from __future__ import annotations

import argparse
import sys
from html import escape

from contacts import build_buying_group
from enrich import enrich_deal
from src.config import env, load_config
from src.deal_naming import build_deal_name, primary_contractor
from src.framework import resolve_framework
from src.hubspot_client import HubSpotClient
from src.note_body import SUMMARY_NOTE_MARKER, render_summary_body
from src.notion_client import NotionClient

TLDR_PROPERTY = "Enrichment summary"


def _prop_checkbox(row: dict, name: str) -> bool:
    return bool(((row.get("properties") or {}).get(name) or {}).get("checkbox"))


def _prop_rich(row: dict, name: str) -> str:
    rich = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return rich[0].get("plain_text", "") if rich else ""


def _prop_rich_all(row: dict, name: str) -> str:
    """Every rich_text segment of a property, joined.

    Notion splits a long value across segments (2000 chars each), so
    _prop_rich()'s first-segment read silently loses the tail — harmless for
    General contractor/JV or Location, wrong for a summary, which is exactly
    the trap backfill_uk_sweep_notes_bodies.py had to work around on the UK
    sweep (7 of 109 notes ran past one segment).
    """
    rich = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in rich)


def _prop_select(row: dict, name: str) -> str:
    sel = ((row.get("properties") or {}).get(name) or {}).get("select") or {}
    return sel.get("name", "")


def _prop_url(row: dict, name: str) -> str:
    return ((row.get("properties") or {}).get(name) or {}).get("url") or ""


def _prop_multi(row: dict, name: str) -> list[str]:
    items = ((row.get("properties") or {}).get(name) or {}).get("multi_select") or []
    return [i.get("name", "") for i in items if i.get("name")]


def _gate_signals(notion: NotionClient, row: dict) -> dict:
    """The fields the FieldAtlas gate reads (rule 20), in one place so the
    enrich and contacts paths cannot drift apart and disagree."""
    title = notion.row_title(row)
    return {
        "region": _prop_select(row, "Region"),
        "country": _prop_rich(row, "Country"),
        "stage": _prop_select(row, "Project stage"),
        "use_cases": _prop_multi(row, "Use case"),
        "fit_profile": _prop_select(row, "Fit profile"),
        "text": f"{title} {_prop_rich(row, 'Summary')}",
    }


def _push_summary_note(hubspot: HubSpotClient, row: dict, *, deal_id: str,
                       deal_name: str, page_url: str, fresh_deal: bool) -> None:
    """Put the Notion summary on the deal as a note, exactly once.

    Prefers `Enrichment summary` (the research pack TL;DR) and falls back to
    the ingest `Summary`, so a row whose deal is created before Enrich has
    ever run still lands in HubSpot with readable notice text.

    Idempotency keys on SUMMARY_NOTE_MARKER in the deal's existing notes, not
    on `fresh_deal`: a run that creates the deal and then dies on the note
    leaves the box checked, and the retry sees an existing deal — keying off
    creation alone would drop the note forever. `fresh_deal` only decides the
    fallback when the notes read itself fails (a token missing
    crm.objects.notes.read 403s here): write on the run that made the deal,
    stay quiet on later ones rather than risking duplicates.

    That same marker is stamped by enrich.py's pack note, which is what keeps
    both boxes ticked on one row from producing two copies of the summary.
    `Enrich` runs first (ACTIONS order) and its note carries the freshly
    extracted TL;DR, so this path correctly stands down — the `row` dict here
    is the pre-run snapshot and would otherwise fall all the way back to the
    stale ingest `Summary`.
    """
    summary, source = _prop_rich_all(row, TLDR_PROPERTY), "research pack TL;DR"
    if not summary.strip():
        summary, source = _prop_rich_all(row, "Summary"), "notice summary"
    if not summary.strip():
        print(f"No summary on the Notion row for '{deal_name[:60]}' — "
              f"deal left without a summary note.")
        return

    try:
        already = hubspot.deal_has_note_marker(deal_id, SUMMARY_NOTE_MARKER)
    except Exception as exc:
        if not fresh_deal:
            print(f"WARNING: could not read existing notes on deal {deal_id} "
                  f"({exc}) — skipping the summary note rather than risking a "
                  f"duplicate.", file=sys.stderr)
            return
        already = False
    if already:
        print(f"Deal {deal_id} already carries a {SUMMARY_NOTE_MARKER} note — "
              f"not adding a second.")
        return

    body = render_summary_body(summary, source)
    if page_url:
        body += (f"<p>Notion row: <a href=\"{escape(page_url, quote=True)}\">"
                 f"{escape(deal_name)}</a></p>")
    hubspot.add_note(deal_id, body, pin=True)
    print(f"Added the {source} to deal {deal_id} as a pinned note.")


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
        gc = _prop_rich(row, "General contractor/JV")
        location = _prop_rich(row, "Location")
        deal_name = build_deal_name(title, contractor=gc, location=location)
        deal = hubspot.create_deal(name=deal_name, notice_id=notice_id, ae=ae)
        deal_id = deal["id"]
        print(f"Created deal {deal_id} to carry the research pack: {deal['portal_url']}")

    # This path has the whole row, so it supplies every gate signal (rule 20).
    enrich_deal(cfg, hubspot, deal_id=deal_id, deal_name=deal_name, notice_id=notice_id,
               ae=ae, country=country, notice_url=notice_url,
               gate=_gate_signals(notion, row), slack=None)


def _run_create_deal(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    notice_id = _prop_rich(row, notion.cfg["notice_id_property"])
    ae = _prop_select(row, "AE") or "unassigned"
    gc = _prop_rich(row, "General contractor/JV")
    location = _prop_rich(row, "Location")
    deal_name = build_deal_name(title, contractor=gc, location=location)

    existing = hubspot.find_deal_by_notice_id(notice_id)
    if existing:
        deal_id = existing["id"]
        fresh_deal = False
        print(f"Deal already exists ({deal_id}) for '{title[:60]}' — no new deal created.")
    else:
        tldr = _prop_rich_all(row, TLDR_PROPERTY)  # backfilled if Enrich already ran
        deal = hubspot.create_deal(name=deal_name, notice_id=notice_id, ae=ae,
                                   summary=tldr or None)
        deal_id = deal["id"]
        fresh_deal = True
        print(f"Created deal {deal_id}: {deal['portal_url']}")

    # Deal link first, note second: a note failure leaves the box checked and
    # the next run re-runs both, but the link is the more expensive thing to
    # lose in the meantime (it's how a human gets from the row to the deal).
    notion.update_properties(row["id"], {
        "HubSpot deal": {"url": f"https://app.hubspot.com/contacts/"
                                f"{cfg['hubspot']['portal_id']}/deal/{deal_id}"}})

    _push_summary_note(hubspot, row, deal_id=deal_id, deal_name=deal_name,
                       page_url=row.get("url", ""), fresh_deal=fresh_deal)


def _run_create_lead(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    notice_id = _prop_rich(row, notion.cfg["notice_id_property"])
    ae = _prop_select(row, "AE") or "unassigned"
    gc = _prop_rich(row, "General contractor/JV")
    location = _prop_rich(row, "Location")
    if not gc:
        raise RuntimeError("General contractor/JV is blank — a HubSpot lead has "
                           "to hang off a company, so there is nothing to create "
                           "yet. Fill it in and the box retries next run.")

    # Before the dedup search, not after: the search filters on the notice-id
    # property, and searching a property the leads object doesn't have yet is
    # a 400, not an empty result.
    hubspot.ensure_lead_notice_property()

    existing = hubspot.find_lead_by_notice_id(notice_id)
    if existing:
        lead_id = existing["id"]
        print(f"Lead already exists ({lead_id}) for '{title[:60]}' — no new lead created.")
    else:
        # The raw GC/JV string for the NAME (so the lead and the deal for one
        # project read identically, and split_deal_name still parses it), the
        # primary contractor alone for the COMPANY record — '(JV: ...)' has no
        # business in a company name.
        lead_name = build_deal_name(title, contractor=gc, location=location)
        company = hubspot.find_or_create_company(primary_contractor(gc))
        lead = hubspot.create_lead(name=lead_name, notice_id=notice_id, ae=ae,
                                  company_id=company["id"])
        lead_id = lead["id"]
        print(f"Created lead {lead_id} on company {company['id']}: {lead['portal_url']}")

    notion.update_properties(row["id"], {
        "HubSpot lead": {"url": f"https://app.hubspot.com/contacts/"
                                f"{cfg['hubspot']['portal_id']}/record/0-136/{lead_id}"}})


def _run_contacts(cfg: dict, notion: NotionClient, hubspot: HubSpotClient, row: dict) -> None:
    title = notion.row_title(row)
    # build_buying_group() has no dedup of its own — it always searches
    # Amplemarket and creates a fresh list. The checkbox is the only guard
    # against re-firing, and a human re-ticking it after Contacts list is
    # already set (real incident, Aug 2026: Bouygues/PORR/Bechtel each got
    # 4 duplicate lists from repeated ticks) must be a no-op, not another
    # list. Untick manually + clear Contacts list to force a genuine rebuild.
    existing_list = _prop_url(row, "Contacts list")
    if existing_list:
        print(f"Contacts list already exists for '{title[:70]}' ({existing_list}) — "
             f"skipping, not building a duplicate.")
        return

    gc = _prop_rich(row, "General contractor/JV")
    country = _prop_rich(row, "Country")
    ae = _prop_select(row, "AE") or "aled"
    if not gc:
        raise RuntimeError("General contractor/JV is blank — fill it in before requesting contacts.")

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

    # Same signals as _run_enrich, via the same helper — a modular pack matched
    # against concrete personas is the failure mode (see src/framework.py).
    framework = resolve_framework(cfg, **_gate_signals(notion, row))
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
    ("Create lead", _run_create_lead),
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
