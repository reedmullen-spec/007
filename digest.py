"""007 tender digest — scan TED + Find a Tender, filter, dedup, post to Slack.

Usage:
    python digest.py               # live: posts cards to Slack
    python digest.py --dry-run     # prints matches, posts nothing
"""
from __future__ import annotations

import argparse
import sys

from src import state
from src.config import env, load_config
from src.filtering import filter_projects
from src.hubspot_client import HubSpotClient
from src.models import Project
from src.routing import resolve_ae
from src.slack_client import SlackClient
from src.sources import austender, fts, sam, ted


def _fmt_value(p: Project) -> str:
    if p.value is None:
        return "value n/a"
    symbol = "£" if p.currency == "GBP" else "€"
    return f"{symbol}{p.value:,.0f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days-back", type=int, default=2)
    args = parser.parse_args()

    cfg = load_config()
    seen = state.load("seen")

    projects: list[Project] = []
    for name, source in (("TED", ted), ("FTS", fts),
                         ("AUSTENDER", austender), ("SAM", sam)):
        try:
            batch = source.fetch(cfg, days_back=args.days_back)
            print(f"{name}: {len(batch)} notices fetched")
            projects.extend(batch)
        except Exception as exc:
            print(f"WARNING: {name} fetch failed: {exc}", file=sys.stderr)

    candidates = [p for p in filter_projects(projects, cfg) if p.dedup_key not in seen]
    print(f"{len(candidates)} new candidates after filtering + local dedup")

    if args.dry_run:
        for p in candidates:
            print(f"  [{p.source}] {p.title} | {p.buyer} | {p.country} | {_fmt_value(p)}")
        return 0

    slack = SlackClient(env("SLACK_BOT_TOKEN"))
    hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)
    ae_slack = cfg["slack"].get("ae_slack_ids", {})
    news_channels = cfg["slack"].get("news_channels", {})
    region_mentions = cfg["slack"].get("region_mentions", {})

    def card_targets(p, ae):
        """Channel(s) + mention(s) per source: TED/FTS -> EMEA + AE;
        AUSTENDER -> APAC + Jeremy; SAM -> east/west + Jamie/Alex."""
        if p.source == "AUSTENDER":
            region = "au"
        elif p.source == "SAM":
            region = getattr(p, "us_side", "us_east")
        else:
            return [cfg["slack"]["tender_channel_id"]], [ae_slack.get(ae, "")]
        raw = news_channels.get(region) or cfg["slack"]["tender_channel_id"]
        chans = raw if isinstance(raw, list) else [raw]
        names = region_mentions.get(region, [])
        return chans, [ae_slack.get(n, "") for n in names]

    posted = 0
    for p in candidates:
        # CRM pre-check: HubSpot is the source of truth for dedup.
        try:
            existing = hubspot.find_deal_by_notice_id(p.notice_id)
        except Exception as exc:
            print(f"WARNING: HubSpot pre-check failed for {p.notice_id}: {exc}",
                  file=sys.stderr)
            existing = None
        if existing:
            seen[p.dedup_key] = {"skipped": "already in HubSpot",
                                 "deal_id": existing.get("id")}
            continue

        ae = resolve_ae(p.buyer, p.country, cfg, hubspot)
        lines = [
            f"Buyer: {p.buyer or 'n/a'}",
            f"Country: {p.country or 'n/a'} · {_fmt_value(p)}",
            f"CPV: {', '.join(p.cpv_codes[:4]) or 'n/a'}",
        ]
        if p.deadline:
            lines.append(f"Deadline: {p.deadline}")
        if ae:
            lines.append(f"Suggested AE: {ae.capitalize()}")

        meta = {"k": p.dedup_key, "nid": p.notice_id, "t": p.title[:120],
                "ae": ae, "src": p.source, "cp": 1, "country": p.country}
        chans, mentions = card_targets(p, ae)
        stamps = []
        for channel in chans:
            ts = slack.post_card(channel, f"[{p.source}] {p.title}", lines, meta,
                                 link=p.url, mention=mentions)
            stamps.append(ts)
            posted += 1
        seen[p.dedup_key] = {"ts": stamps}

    state.save("seen", seen)
    print(f"Posted {posted} cards to Slack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
