"""007 step 2 — background research pack. Standalone-invokable.

The HubSpot deal (keyed on tender_notice_id) is the shared state object, so
this step does NOT need step 1 to have run. Three ways in:

  python enrich.py --deal-id 12345678
      Research an existing deal (whatever created it).

  python enrich.py --title "Contractor — Project" --country BE [--ae lisa]
      No deal yet: dedup-checks, creates the deal, then researches it.
      A manual notice ID is derived (MANUAL:<slug>) so dedup still works.

  python enrich.py --notice-id ocds-h6vhtk-xxxx
      Finds the deal carrying that notice ID and researches it.

Output: Notion row (found-or-created on notice ID) with the pack in the
body, a pinned HubSpot note linking to it, and a checkpoint-2 Slack card
asking whether to build contacts (✅ = run step 3, done by approvals.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from src.config import env, load_config
from src.anthropic_client import run_research
from src.hubspot_client import HubSpotClient
from src.notion_client import NotionClient
from src.routing import resolve_ae
from src.slack_client import SlackClient


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def resolve_target(args, hubspot: HubSpotClient, cfg: dict) -> tuple[str, str, str, str]:
    """Return (deal_id, deal_name, notice_id, ae)."""
    if args.deal_id:
        deal = hubspot.get_deal(args.deal_id)
        props = deal.get("properties", {})
        notice_id = props.get(cfg["hubspot"]["notice_id_property"]) or f"MANUAL:{_slug(props.get('dealname',''))}"
        ae = _ae_from_owner(props.get("hubspot_owner_id"), cfg) or "aled"
        return args.deal_id, props.get("dealname", ""), notice_id, ae

    if args.notice_id:
        existing = hubspot.find_deal_by_notice_id(args.notice_id)
        if not existing:
            raise SystemExit(f"No deal found with notice id {args.notice_id}. "
                             f"Use --title/--country to create one.")
        props = existing.get("properties", {})
        deal = hubspot.get_deal(existing["id"])
        ae = _ae_from_owner(deal.get("properties", {}).get("hubspot_owner_id"), cfg) or "aled"
        return existing["id"], props.get("dealname", ""), args.notice_id, ae

    if args.title:
        notice_id = f"MANUAL:{_slug(args.title)}"
        existing = hubspot.find_deal_by_notice_id(notice_id)
        if existing:
            print(f"Deal already exists for this title (id {existing['id']}); using it.")
            deal_id = existing["id"]
            ae = args.ae or resolve_ae(args.title, args.country or "", cfg, hubspot)
        else:
            ae = args.ae or resolve_ae(args.title, args.country or "", cfg, hubspot)
            deal = hubspot.create_deal(name=args.title, notice_id=notice_id, ae=ae)
            deal_id = deal["id"]
            print(f"Created deal {deal_id}: {deal['portal_url']}")
        return deal_id, args.title, notice_id, ae

    raise SystemExit("Provide --deal-id, --notice-id, or --title (with --country).")


def _ae_from_owner(owner_id: str | None, cfg: dict) -> str | None:
    for ae, oid in cfg["hubspot"]["owners"].items():
        if owner_id and str(oid) == str(owner_id) and ae != "reed":
            return ae
    return None


def enrich_deal(cfg: dict, hubspot: HubSpotClient, *, deal_id: str,
                deal_name: str, notice_id: str, ae: str,
                country: str = "", notice_url: str = "",
                slack: SlackClient | None = None) -> str:
    """Run research -> Notion -> HubSpot note -> checkpoint-2 card.
    Returns the Notion page URL. Reused by approvals.py after checkpoint 1."""
    framework = cfg["enrichment"]["framework_by_ae"].get(ae, "concretedna")
    print(f"Researching '{deal_name}' with the {framework} framework…")
    pack = run_research(env("ANTHROPIC_API_KEY"), cfg, title=deal_name,
                        country=country, framework=framework,
                        notice_url=notice_url)

    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    row = notion.find_or_create_row(deal_name, notice_id)
    notion.append_pack(row["id"], pack)
    page_url = row.get("url", "")
    print(f"Notion row: {page_url}")

    hubspot.add_note(
        deal_id,
        f"<p><strong>007 research pack</strong>: "
        f"<a href=\"{page_url}\">{deal_name}</a> (framework: {framework})</p>",
        pin=True,
    )

    if slack is not None:
        channel = cfg["slack"]["tender_channel_id"]
        skip = country.upper() in [c.upper() for c in cfg.get("hakron_skip_contacts_countries", [])]
        meta = {"k": notice_id, "nid": notice_id, "t": deal_name[:120],
                "ae": ae, "src": "CP2", "cp": 2, "deal": deal_id,
                "country": country}
        lines = [f"Research pack ready: {page_url}",
                 f"Deal: https://app.hubspot.com/contacts/{cfg['hubspot']['portal_id']}/deal/{deal_id}"]
        if skip:
            lines.append("Belgium/Hakron path — contact build will be SKIPPED; "
                         "Lisa carries the pack to Hakron.")
        else:
            lines.append("React ✅ to build the 15–20 contact buying group in Amplemarket.")
        slack.post_card(channel, f"[CHECKPOINT 2] {deal_name}", lines, meta)
    return page_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id")
    parser.add_argument("--notice-id")
    parser.add_argument("--title")
    parser.add_argument("--country", default="")
    parser.add_argument("--ae", choices=["lisa", "aled", "avi"])
    parser.add_argument("--no-slack", action="store_true",
                        help="Skip posting the checkpoint-2 card")
    args = parser.parse_args()

    cfg = load_config()
    hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)
    hubspot.ensure_notice_property()
    slack = None if args.no_slack else SlackClient(env("SLACK_BOT_TOKEN"))

    deal_id, deal_name, notice_id, ae = resolve_target(args, hubspot, cfg)
    if args.ae:
        ae = args.ae
    enrich_deal(cfg, hubspot, deal_id=deal_id, deal_name=deal_name,
                notice_id=notice_id, ae=ae, country=args.country, slack=slack)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
