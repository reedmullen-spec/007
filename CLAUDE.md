# 007 — Tender & Project Radar (Converge)

Read this before changing anything. It encodes decisions with reasons;
several look arbitrary but are load-bearing.

## What this is
Automated pipeline that finds construction projects (tenders + news),
qualifies them with a cheap Claude call, stores them as structured rows in a
Notion database, and runs a weekly per-region triage that tells the team
which projects to work. Deep research and HubSpot deal creation happen
on-demand. Runs entirely on GitHub Actions — no servers.

Owner: Reed Mullen (SDR). The strategic history lives in Reed's Claude chat;
ask Reed before changing architecture, not just code.

## Architecture (current, V1)
- `ingest.py` — daily 02:00 UTC. Fetch TED (EU) + FTS (UK) + AusTender (AU
  federal awards) + SAM.gov (US federal; needs SAM_API_KEY, skips politely
  without it) + news RSS (trade press + ~84 generated Google News contractor
  queries from `watchlist.yaml`). Filter → dedup → qualify (one small
  Anthropic API call per candidate: summary, canonical GC, project type,
  phase, expected concrete start, location, low/med/high fit + one-line
  reason) → geocode (Nominatim, cached in state) → create Notion row,
  Status=New. Also sweeps rows humans added by hand (no Notice ID):
  qualifies them and stamps Source=MANUAL.
  Backfill: `--historical --days-back 365 --max-rows 300`, run repeatedly;
  dedup makes it self-continuing. News RSS cannot backfill (feeds are shallow).
- `triage.py` — Monday 06:00 UTC. Top-N New rows per region (config
  `triage.per_region`), ranked MANUAL-first (human/White Cap intel beats
  scraped), then fit, then value. Carry-over: "This week" rows persist and
  consume slots. Flips picks to "This week", posts one Slack list per region
  with Notion links.
- `enrich.py` — deep research (step 2). Standalone: `--deal-id` / `--title` /
  `--notice-id`. Anthropic API + web search, system prompt = the SKILL.md
  for the framework (concretedna for Lisa/Aled, fieldatlas for Avi). Writes
  pack to Notion, pins link on HubSpot deal, posts checkpoint-2 Slack card.
  Packs must start with a 5-bullet TL;DR, max 600 words (AE feedback).
- `contacts.py` — step 3, Amplemarket buying group (15–20, persona titles by
  framework). Belgium refuses by design (see Hakron). Enginy may replace
  Amplemarket later — treat this module as a swap point, don't extend it.
- `approvals.py` — several times daily. Processes ✅ reactions on cards.
- `digest.py` / `news.py` — legacy Slack card flows. Schedules retired
  (workflow_dispatch only); superseded by ingest+triage. Don't delete yet.

## Rules that look arbitrary but aren't
1. **Never read Slack channel history.** CTO requirement. The bot records
   every card it posts in `state/cards.json` and checks reactions with
   `reactions.get` on its own messages only. Do NOT add `channels:history`
   or `conversations.history` calls. Scopes: chat:write, reactions:read,
   reactions:write. That's the full set.
2. **HubSpot is dedup truth for deals** via custom deal property
   `tender_notice_id`. State files are a secondary cache. Name-based
   overlap is NOT caught — the human ✅ is the guard (known, accepted).
3. **Belgium = Hakron partner path.** Full research pack, NO contact build
   (Lisa carries packs to Hakron). `hakron_skip_contacts_countries: [BE]`.
4. **`news_channels` values can be a string OR a list** (national US firms
   post to both east+west). Anything iterating channels must handle both —
   this has caused a crash once already.
5. **All workflows share concurrency group `tender-radar-state`** so state
   commits can't race. Every job needs `timeout-minutes` (a hung run once
   deadlocked all schedules) and the state-commit step needs `if: always()`.
6. **News dedup is keyed on normalized HEADLINE, not URL** (same story
   arrives via multiple feeds with different redirect URLs), plus a >0.6
   token-Jaccard near-duplicate collapse.
7. **SAM.gov must require an include-keyword hit + sam_exclude_keywords** —
   NAICS matches by construction and once flooded 233 junk notices.
8. **Notion access is page-by-page.** The Background project research page
   (parent, in the MI6 teamspace) must be Connected to the "007 Data
   Storage" integration. The "007 Projects" database is auto-created inside
   it (`ensure_database`, cached in state/notion.json) and its schema is
   auto-patched idempotently (`ensure_schema`).
9. **Keyword gates are word-boundary matched** ("contract" must not match
   "contractors" — real incident).

## Confirmed IDs (do not guess)
- HubSpot: portal 2061231, Sales Pipeline 21257366, Identified stage
  1326060402. Owners: Reed 90628877, Lisa 465940403, Aled 146637928,
  Avi 32656681.
- Slack channels: EMEA C0BGRLQ2BMH, US-East C0BJS20BUDB, US-West
  C0BJXLBT8RF, Canada C0BK21UMHK3, APAC C0BJX8B028Z.
- Slack people: Reed U0AS3P6EY80 (approver), Lisa U05G8DX4B0A, Aled
  U07GNGNMQGZ, Avi UB96Q98T0, Alex U0AKM99CK08, Jamie U32ML88RE,
  Jeremy U01FP4CRR8A.
- GitHub secrets (names are load-bearing): SLACK_BOT_TOKEN, HUBSPOT_TOKEN,
  ANTHROPIC_API_KEY, NOTION_TOKEN, AMPLEMARKET_TOKEN, SAM_API_KEY (optional).
- Notion parent page: 3a6a315b1b0080bdb2b2fae4c805d40e.

## Routing (confirmed by Guillaume, Aug 2026)
UK non-strategic → Aled; Europe → Lisa; Italy → Aled; European-owned UK
contractors (BAM, BESIX, Strabag, VolkerWessels, Jan de Nul, DEME) → Lisa.
Tier 1 of the resolver: live HubSpot company ownership overrides geography.
US: East → Jamie, West + Canada → Alex, national firms → both, APAC →
Jeremy. US East/West state split in `sam.py` WEST_STATES is an
approximation — Darren is producing the definitive map; update when it lands.

## Statuses (Notion select; drive the future map colours)
New (grey) → This week (blue) → Working on (amber) / Recontact later
(hollow amber, has date) → On the project (green) / Disqualified / Lost
(both red — kept separate for partner conversations, never delete rows).

## Roadmap context
- V2: static HTML portal generated from the Notion DB — list + Rightmove-
  style Leaflet map (lat/lng already captured at ingest), status colours,
  pin size by value, red hidden by default. Cloudflare Access,
  converge.io-only. Gideon (security) must approve hosting.
- Weekly agency handoff (Guillaume): ~100 prioritised EU/Nordic/Baltic
  projects/month to an outbound agency; they book meetings.
- Known debt: GC canonicalisation is prompt-enforced only (not yet
  cross-checked against HubSpot company records); news-headline rows are
  thinner than tender rows until deep-researched.

## Testing conventions
Compile-check everything (`python -m py_compile`), test logic offline with
fake clients (see how routing/triage/slack tests were done), never hit live
APIs in tests. Dry-run flags exist on every entry point — keep it that way.
