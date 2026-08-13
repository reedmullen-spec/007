"""007 step 3 — Amplemarket buying-group build. Standalone-invokable.

Does NOT require step 2 to have run: all it needs is a contractor to search
on. Two ways in:

  python contacts.py --deal-id 12345678 [--company "BESIX"]
      Uses the deal. The contractor is taken from --company if given,
      otherwise parsed from the deal name (src/deal_naming.py's
      '[Contractor] — [Project] — [Location]' convention).

  python contacts.py --company "Renaker" --project "Thames City" --ae aled
      No deal at all — pure list build.

Searches Amplemarket for people at the contractor matching the framework's
persona titles (plus the project name as a profile keyword to catch people
who mention it), and creates a shared lead list named after the deal.

Belgium/Hakron path: refuses by default (pack goes to Hakron via Lisa);
override with --force if you really want contacts anyway.
"""
from __future__ import annotations

import argparse
import sys

from src.amplemarket_client import AmplemarketClient
from src.config import env, load_config
from src.deal_naming import split_deal_name
from src.hubspot_client import HubSpotClient
from src.notion_client import NotionClient


def _split_name(person: dict) -> tuple[str, str]:
    """Amplemarket's /people/search result shape for a person's name isn't
    pinned down anywhere in this codebase yet (create_lead_list only ever
    needed linkedin_url/title/company) — try the field names people-search
    APIs commonly use rather than assume one and crash on the others."""
    first = person.get("first_name") or ""
    last = person.get("last_name") or ""
    if first or last:
        return first, last
    full = person.get("name") or person.get("full_name") or ""
    parts = full.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full, "")


def _push_to_hubspot(hubspot: HubSpotClient, people: list[dict],
                     company: str, deal_id: str) -> None:
    """Push each matched person into HubSpot as a contact, associated to
    the deal that gated this build (rule: Build contacts requires a deal
    to already exist — see actions.py's _run_contacts). Best-effort per
    contact: one bad record shouldn't sink the rest of the buying group."""
    hubspot.ensure_linkedin_property()
    pushed = 0
    for person in people:
        first, last = _split_name(person)
        try:
            contact = hubspot.upsert_contact(
                email=person.get("email", ""), first_name=first, last_name=last,
                title=person.get("title", ""), company_name=company,
                linkedin_url=person.get("linkedin_url", ""))
            hubspot.associate_default("contacts", contact["id"], "deals", deal_id)
            pushed += 1
        except Exception as exc:
            print(f"WARNING: could not push {person.get('linkedin_url', '(no linkedin)')} "
                 f"to HubSpot: {exc}", file=sys.stderr)
    print(f"Pushed {pushed}/{len(people)} contacts to HubSpot deal {deal_id}")


def build_buying_group(cfg: dict, *, company: str, project: str,
                       framework: str, country: str = "",
                       force: bool = False, hubspot: HubSpotClient | None = None,
                       deal_id: str | None = None) -> dict:
    """Search + create the lead list. Returns the lead list object.
    Reused by approvals.py after checkpoint 2.

    When hubspot + deal_id are given, every matched person is also pushed
    into HubSpot as a contact associated to that deal — the CLI's pure
    --company path (no deal at all) skips this and stays a plain
    Amplemarket-only list build."""
    skip_countries = [c.upper() for c in cfg.get("hakron_skip_contacts_countries", [])]
    if country.upper() in skip_countries and not force:
        raise RuntimeError(
            f"{country}: Hakron partner path — contact build skipped by design. "
            f"Use --force to override.")

    am = AmplemarketClient(env("AMPLEMARKET_TOKEN"), cfg)
    titles = cfg["amplemarket"]["titles"].get(framework, [])
    size = cfg["amplemarket"].get("buying_group_size", 20)

    # Pass 1: people at the company whose profile mentions the project.
    people = am.search_people(company_names=[company], titles=titles,
                              keywords=[project] if project else None,
                              limit=size)
    # Pass 2: top up with title-matched people at the company generally.
    if len(people) < size:
        seen = {p.get("linkedin_url") for p in people}
        extra = am.search_people(company_names=[company], titles=titles,
                                 limit=size)
        people += [p for p in extra if p.get("linkedin_url") not in seen]
    people = people[:size]

    if not people:
        raise RuntimeError(f"No Amplemarket matches at '{company}' for the "
                           f"{framework} persona titles.")

    list_name = f"007 — {company} — {project}" if project else f"007 — {company}"
    result = am.create_lead_list(name=list_name, people=people)
    print(f"Lead list created ({len(people)} leads): {result.get('url', result.get('id'))}")

    if hubspot is not None and deal_id:
        _push_to_hubspot(hubspot, people, company, deal_id)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id")
    parser.add_argument("--company")
    parser.add_argument("--project", default="")
    parser.add_argument("--country", default="")
    parser.add_argument("--ae", choices=[a for a in NotionClient.AES if a != "unassigned"],
                        default="aled")
    parser.add_argument("--force", action="store_true",
                        help="Build contacts even on the Hakron path")
    args = parser.parse_args()

    cfg = load_config()
    company, project, framework = args.company, args.project, None
    hubspot = None

    if args.deal_id:
        hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)
        deal = hubspot.get_deal(args.deal_id)
        props = deal.get("properties", {})
        deal_name = props.get("dealname", "")
        if not company and "—" in deal_name:
            company, project = split_deal_name(deal_name)
        elif not company:
            raise SystemExit(
                f"Deal name '{deal_name}' has no contractor part yet "
                f"(convention: '[Contractor] — [Project] — [Location]'). "
                f"Pass --company.")
        project = project or deal_name
        for ae, oid in cfg["hubspot"]["owners"].items():
            if str(oid) == str(props.get("hubspot_owner_id")) and ae != "reed":
                args.ae = ae

    if not company:
        raise SystemExit("Provide --deal-id or --company.")

    framework = cfg["enrichment"]["framework_by_ae"].get(args.ae, "concretedna")
    build_buying_group(cfg, company=company, project=project,
                       framework=framework, country=args.country,
                       force=args.force, hubspot=hubspot, deal_id=args.deal_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
