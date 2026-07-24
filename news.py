"""007 news radar — scan RSS feeds, keyword-gate, route by region, post cards.
 
Google News search feeds (pre-filtered by their query) bypass the keyword
gate; whole-site trade feeds are gated. Cards carry the same 007 metadata as
tender cards, so a ✅ reaction on a news card creates a HubSpot deal too.
 
Usage:
    python news.py               # live
    python news.py --dry-run     # print matches only
"""
from __future__ import annotations
 
import argparse
import sys
 
from src import state
from src.config import env, load_config, load_feeds
from src.models import NewsItem
from src.routing import resolve_ae_verbose
from src.slack_client import SlackClient
 
REGION_COUNTRY = {"uk": "GB", "eu": "", "us": "US", "ca": "CA", "au": "AU"}
REGION_LOCALE = {"uk": ("en-GB", "GB", "GB:en"),
                 "eu": ("en", "BE", "BE:en"),
                 "us": ("en-US", "US", "US:en"),
                 "us_east": ("en-US", "US", "US:en"),
                 "us_west": ("en-US", "US", "US:en"),
                 "ca": ("en-CA", "CA", "CA:en"),
                 "au": ("en-AU", "AU", "AU:en")}
MAX_ITEMS_PER_FEED = 10
 
 
def _word_hit(title: str, keywords: list[str]) -> bool:
    """Word-boundary match: 'contract' must not match 'contractors'."""
    import re
    lowered = title.lower()
    return any(re.search(r"\b" + re.escape(k.lower()) + r"\b", lowered)
               for k in keywords)
 
 
def watchlist_feeds() -> list[dict]:
    """One Google News signal feed per watchlist contractor/project.
 
    Each query is '"{entity}" AND (awarded OR breaking ground OR …)' via the
    template in watchlist.yaml, so these feeds are pre-filtered and bypass
    the keyword gate.
    """
    from pathlib import Path
    from urllib.parse import quote
 
    import yaml
 
    root = Path(__file__).resolve().parent
    with open(root / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
 
    template = " ".join(wl["signal_query_template"].split())
    feeds: list[dict] = []
    for kind in ("contractors", "projects"):
        for entry in wl.get(kind, []):
            region = entry.get("region", "uk")
            hl, gl, ceid = REGION_LOCALE[region]
            hl = entry.get("hl", hl)
            gl = entry.get("gl", gl)
            ceid = entry.get("ceid", ceid)
            query = template.format(entity=entry["name"])
            feeds.append({
                "region": region,
                "entity": entry["name"],
                "url": (f"https://news.google.com/rss/search?"
                        f"q={quote(query)}&hl={hl}&gl={gl}&ceid={quote(ceid)}"),
                "keyword_gate": False,
            })
    return feeds
 
 
def _too_old(entry, max_age_days: int) -> bool:
    """Drop entries with no parseable date or older than the cutoff."""
    import calendar, time
    parsed = getattr(entry, "published_parsed", None) or \
             getattr(entry, "updated_parsed", None)
    if not parsed:
        return True
    age_secs = time.time() - calendar.timegm(parsed)
    return age_secs > max_age_days * 86400
 
 
def collect(cfg: dict) -> list[NewsItem]:
    import feedparser  # deferred: installed via requirements.txt in Actions
 
    max_age = cfg["news"].get("max_age_days", 7)
    excludes = cfg["news"].get("exclude_keywords", [])
    items: list[NewsItem] = []
    for feed in load_feeds() + watchlist_feeds():
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as exc:
            print(f"WARNING: feed failed {feed.get('entity')}: {exc}", file=sys.stderr)
            continue
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title = getattr(entry, "title", "") or ""
            link = getattr(entry, "link", "") or ""
            if not title or not link:
                continue
            if _too_old(entry, max_age):
                continue
            if _word_hit(title, excludes):
                continue
            if feed.get("keyword_gate") and not _word_hit(title, cfg["news"]["gate_keywords"]):
                continue
            items.append(NewsItem(
                region=feed["region"],
                entity=feed.get("entity", ""),
                title=title,
                url=link,
                published=getattr(entry, "published", ""),
            ))
    return items
 
 
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
 
    cfg = load_config()
    seen = state.load("seen")
 
    fresh = [i for i in collect(cfg) if i.dedup_key not in seen]
 
    # Near-duplicate collapse: same story worded differently across feeds.
    items = []
    for cand in fresh:
        dup = False
        for kept in items:
            inter = cand.title_tokens & kept.title_tokens
            union = cand.title_tokens | kept.title_tokens
            if union and len(inter) / len(union) > 0.6:
                dup = True
                break
        if not dup:
            items.append(cand)
    print(f"{len(items)} new news items after gate + dedup "
          f"({len(fresh) - len(items)} near-duplicates collapsed)")
 
    if args.dry_run:
        for i in items:
            print(f"  [{i.region}] {i.entity}: {i.title}")
        return 0
 
    slack = SlackClient(env("SLACK_BOT_TOKEN"))
    channels = cfg["slack"]["news_channels"]
    ae_slack = cfg["slack"].get("ae_slack_ids", {})
 
    # HubSpot connection for tier-1 ownership routing (e.g. which UK
    # companies belong to Aled vs Lisa). Optional: without HUBSPOT_TOKEN
    # the radar still runs on geography alone.
    import os
    hubspot = None
    if os.environ.get("HUBSPOT_TOKEN"):
        from src.hubspot_client import HubSpotClient
        hubspot = HubSpotClient(os.environ["HUBSPOT_TOKEN"], cfg)
 
    import time as _time
    import datetime as _dt
    cap = cfg["news"].get("max_cards_per_run", 25)
    week = _dt.date.today().strftime("%d %b %Y")
    parents: dict[str, str] = {}   # channel -> weekly parent ts
 
    def parent_for(channel: str) -> str:
        if channel not in parents:
            parents[channel] = slack.post_parent(
                channel, f"Weekly announced deals — w/c {week}")
            _time.sleep(1)
        return parents[channel]
 
    posted = 0
    for i in items:
        if posted >= cap:
            print(f"Per-run cap of {cap} reached — "
                  f"{len(items)} candidates total, rest carry to next run.")
            break
        base_region = i.region.split("_")[0]
        raw = (channels.get(i.region) or channels.get(base_region)
               or cfg["slack"]["tender_channel_id"])
        targets = raw if isinstance(raw, list) else [raw]
        country = REGION_COUNTRY.get(base_region, "")
        ae, from_hubspot = resolve_ae_verbose(i.entity, country, cfg, hubspot)
 
        # Mentions: if HubSpot ownership decided the AE, tag them alone.
        # Otherwise use the region map (uk -> both Aled and Lisa, etc.),
        # falling back to the geographic AE.
        region_mentions = cfg["slack"].get("region_mentions", {})
        if from_hubspot:
            mentions = [ae_slack.get(ae, "")]
        else:
            names = region_mentions.get(i.region, [])
            mentions = [ae_slack.get(n, "") for n in names] or [ae_slack.get(ae, "")]
 
        meta = {"k": i.dedup_key, "nid": i.dedup_key, "t": i.title[:120],
                "ae": ae, "src": "NEWS", "cp": 1,
                "country": country}
        region_line = f"Region: {i.region.upper()}"
        if ae:
            region_line += f" · Suggested AE: {ae.capitalize()}"
        lines = [f"Entity: {i.entity}", region_line]
        if i.published:
            lines.append(f"Published: {i.published}")
        stamps = []
        for channel in targets:
            ts = slack.post_card(channel, f"[NEWS] {i.title}", lines, meta,
                                 link=i.url, mention=mentions,
                                 thread_ts=parent_for(channel))
            stamps.append(ts)
            posted += 1
            _time.sleep(1)   # stay under Slack's rate limit
        seen[i.dedup_key] = {"ts": stamps}
 
    state.save("seen", seen)
    print(f"Posted {posted} news cards")
    return 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())
 
