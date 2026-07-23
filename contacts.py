"""007 step 3 — Amplemarket buying-group build. Standalone-invokable.

Does NOT require step 2 to have run: all it needs is a contractor to search
on. Two ways in:

  python contacts.py --deal-id 12345678 [--company "BESIX"]
      Uses the deal. The contractor is taken from --company if given,
      otherwise parsed from the deal name ('[Contractor] — [Project]').

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
from src.hubspot_client import HubSpotClient


def build_buying_group(cfg: dict, *, company: str, project: str,
                       framework: str, country: str = "",
                       force: bool = False) -> dict:
    """Search + create the lead list. Returns the lead list object.
    Reused by approvals.py after checkpoint 2."""
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
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id")
    parser.add_argument("--company")
    parser.add_argument("--project", default="")
    parser.add_argument("--country", default="")
    parser.add_argument("--ae", choices=["lisa", "aled", "avi"], default="aled")
    parser.add_argument("--force", action="store_true",
                        help="Build contacts even on the Hakron path")
    args = parser.parse_args()

    cfg = load_config()
    company, project, framework = args.company, args.project, None

    if args.deal_id:
        hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)
        deal = hubspot.get_deal(args.deal_id)
        props = deal.get("properties", {})
        deal_name = props.get("dealname", "")
        # Deal naming convention: '[Contractor] — [Project]'
        if not company and "—" in deal_name:
            company, project = [s.strip() for s in deal_name.split("—", 1)]
        elif not company:
            raise SystemExit(
                f"Deal name '{deal_name}' has no contractor part yet "
                f"(convention: '[Contractor] — [Project]'). Pass --company.")
        project = project or deal_name
        for ae, oid in cfg["hubspot"]["owners"].items():
            if str(oid) == str(props.get("hubspot_owner_id")) and ae != "reed":
                args.ae = ae

    if not company:
        raise SystemExit("Provide --deal-id or --company.")

    framework = cfg["enrichment"]["framework_by_ae"].get(args.ae, "concretedna")
    build_buying_group(cfg, company=company, project=project,
                       framework=framework, country=args.country,
                       force=args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
