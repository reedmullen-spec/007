# 007 — Tender & Project Radar (Converge)

Read this before changing anything. It encodes decisions with reasons;
several look arbitrary but are load-bearing.

## What this is
Automated pipeline that finds construction projects (tenders + news),
qualifies them with a cheap Claude call, stores them as structured rows in a
Notion database, and runs a weekly per-person triage — each of the 13
AEs/SDRs gets their own Monday list (steerable via a weekly focus) and their
own editable Notion page — that tells the team which projects to work. Deep
research and HubSpot deal creation happen on-demand. Runs entirely on GitHub
Actions — no servers.

Owner: Reed Mullen (SDR). The strategic history lives in Reed's Claude chat;
ask Reed before changing architecture, not just code.

## Architecture (current, V1)
- `ingest.py` — weekdays 02:00 + 13:00 UTC. Fetch TED (EU) + FTS (UK) +
  AusTender (AU federal awards) + SAM.gov (US federal; needs SAM_API_KEY,
  skips politely without it) + news RSS (trade press + ~84 generated Google
  News contractor queries from `watchlist.yaml`). Filter → dedup → qualify
  (one small Anthropic API call per candidate: summary, canonical GC,
  project type, work nature, project stage, expected concrete start +
  completion, location, use cases — observations only, no fit; see
  src/scoring.py) → geocode (Nominatim, cached in state) → score → create
  Notion row, Status=New. Also sweeps rows humans added by hand (no Notice
  ID): qualifies + scores them and stamps Source=MANUAL. Also derives
  `SDR` from the resolved `AE` (`routing.ae_sdr_map`) and writes it
  alongside — Alex owns Lawson/Alicia/Ben's patch, Jamie owns
  Britain/Brady/Justin's, Reed's own AEs (Lisa/Aled/Avi/Jeremy) get the
  label too even though he has no dedicated SDR triage list.
  Backfill: `--historical --days-back 365 --max-rows 300`, run repeatedly;
  dedup makes it self-continuing. News RSS cannot backfill (feeds are shallow).
  `backfill_sdr.py` sets `SDR` on rows created before that field existed
  — one-off, AE never changes after ingest so this never needs a repeat.
- `src/scoring.py` — deterministic fit scoring (High/Medium/Low/
  Disqualified) against 5 named profiles; `qualify.py`'s model call
  supplies observations only, never a fit. Deliberately narrow hard
  disqualification (Aug 2026 revision, Issam's biggest accuracy ask): ONLY
  `project_stage == "Complete"` or `Expected completion` under
  `MIN_MONTHS_TO_COMPLETION` (6) away. A past `expected_concrete_start` is
  NOT disqualifying — phased pours (foundations now, a pause, then
  structure later) mean a start up to `PAST_START_GRACE_MONTHS` (12) ago
  still reads as a live site; only the soft "timing" dimension fails
  beyond that, capping the band rather than killing the row. `project_stage`
  is its own dimension (`HIGH_STAGES`: on site / groundbreaking / PCSA /
  awarded → can reach High; still at tender → caps at Medium, real
  pipeline but a Q1+ booking, not this quarter). `resolve_date()` parses
  both qualify's fuzzy text ("Q3 2028") and an exact ISO date read back
  from Notion's `Expected completion` property — needed because
  `rescore_existing.py` re-scores from already-stored fields, not fresh
  qualify output.
- `triage.py` — Monday 06:00 UTC. One list = one person (config
  `triage.lists`: `{name, filter, count, channel}`, filter is `AE equals` /
  `Region equals` / `Product fit contains` — see `build_filter`). Ranked
  MANUAL-first (human/White Cap intel beats scraped), then fit, then value —
  unless the person left a **weekly focus** (see below), in which case a
  small Anthropic call ranks against that instead. Carry-over: "This week"
  rows persist and consume slots; "Recontact later" rows resurface once
  their `Recontact date` has passed. Flips picks to "This week", posts one
  Slack list per list (channel from `slack.news_channels`, mention from
  `slack.ae_slack_ids[name]`), and mirrors that week's facts onto the
  person's own `My week — {Name}` page (`src/ae_pages.py`). Lists are never
  de-duplicated against each other — the SDR squad and the White Cap AEs
  deliberately work the same regions from different angles. **Dan (US Data
  Hub) is deliberately not in `triage.lists`** — per the team, Data Hub is
  handled manually, not through the automated per-AE page/list system. He
  stays a selectable `AE` value in the master schema for manual tagging.
- **Weekly focus** (`src/focus.py`) — a "007 Weekly focus" Notion database,
  one row per person per week (`Person`, `Week starting`, `Focus` text,
  `Applied` checkbox), editable by everyone. Entered before Sunday
  20:00 UTC (`triage.focus_deadline_hour_utc`) → that week's list is ranked
  against it instead of the default order; late or blank → default ranking,
  never an empty list. A narrow focus returns a short list plus an explicit
  "only N matched" line — never padded. `focus_reminder.py` DMs everyone in
  `triage.lists` at ~4pm THEIR local time on Friday (per-person
  `triage.lists[].timezone`, checked hourly via zoneinfo so it survives
  DST without cron edits) with a link to the Focus database, since the
  ritual is pull-only otherwise (nobody's prompted to enter one).
- **Per-AE pages** (`src/ae_pages.py`) — Notion permissions are page-level,
  not row-level, so a filtered view of the read-only master can't be made
  person-editable without exposing every row. Each person instead gets their
  own `My week — {Name}` database (id cached in `state/ae_pages.json`).
  `triage.py` writes facts + `Why this project` there every Monday; the
  person owns `Status` / `Next action` / `Next action date` / `Notes` /
  `Outcome` / `Correction needed`.
- `sync.py` — weekdays 23:00 UTC. Pulls `Status` / `Next action` / `Next
  action date` from each AE page back to the master (one direction only —
  the master never overwrites those fields outside Monday's initial write).
  When the incoming `Status` is "Recontact later", the AE's `Next action
  date` also becomes the master's `Recontact date` — one field, not two.
  `Notes` / `Outcome` / `Correction needed` never sync to the master;
  non-empty `Correction needed` entries are DM'd to Reed once each
  (`state/sync.json` tracks what's already been flagged) so a wrong fact can
  be fixed by a human, never auto-applied.
- `recheck_awards.py` — weekly (Wednesday 04:00 UTC). Most TED/FTS rows
  have no `General contractor` because they're pre-award tender notices —
  the winner genuinely isn't known yet (confirmed live: TED ~1% GC
  capture, FTS ~0%, vs NEWS ~67% since "awarded"/"breaking ground" stories
  inherently name one). This backfills existing rows past their `Tender
  deadline` once an award notice has since appeared. TED: buyer-name
  exact match (a buyer's name doesn't change between their tender and its
  award) + title-token similarity to pick the right award among that
  buyer's others — a fuzzy match, flagged in `Fit reason`, `Verified`
  stays False. FTS: exact OCID match (an award-stage OCDS release shares
  its OCID with the original tender release) — `Verified` set True.
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
10. **The Notion API cannot share a page/database with a specific person** —
    only integration Connections, not per-user invites. Creating the Focus
    database and each `My week — {Name}` database is automatic
    (`_ensure_named_database`, cached in `state/notion.json` /
    `state/ae_pages.json`); Reed must manually share each one with the right
    person the first time it's created (the create log line prints the URL
    as a reminder). Don't try to code around this — there's no endpoint.
11. **AE-owned fields sync one way only.** `triage.py` writes facts to a
    person's page every Monday but never touches `Status`/`Next
    action`/`Next action date`/`Notes`/`Outcome`/`Correction needed` on an
    existing row. `sync.py` writes the reverse (`Status`/`Next
    action`/`Next action date` only) back to the master. Widening either
    direction re-creates the sync-fighting this split was built to avoid.

## Confirmed IDs (do not guess)
- HubSpot: portal 2061231, Sales Pipeline 21257366, Identified stage
  1326060402. Owners: Reed 90628877, Lisa 465940403, Aled 146637928,
  Avi 32656681.
- Slack channels: EMEA C0BGRLQ2BMH, US-East C0BJS20BUDB, US-West
  C0BJXLBT8RF, Canada C0BK21UMHK3, APAC C0BJX8B028Z.
- Slack people: Reed U0AS3P6EY80 (approver), Lisa U05G8DX4B0A, Aled
  U07GNGNMQGZ, Avi UB96Q98T0, Alex U0AKM99CK08, Jamie U32ML88RE,
  Jeremy U01FP4CRR8A, Justin U0AS1A6JZ7Y, Lawson U0BGLKVNG65, Alicia
  U0BA6PQB8CA, Ben U08H37C1Z9S, Britain U08H37AMZ36, Brady U0BMDES0YRW.
- GitHub secrets (names are load-bearing): SLACK_BOT_TOKEN, HUBSPOT_TOKEN,
  ANTHROPIC_API_KEY, NOTION_TOKEN, AMPLEMARKET_TOKEN, SAM_API_KEY (optional).
- Notion parent page: 3a6a315b1b0080bdb2b2fae4c805d40e.

## Routing (confirmed by Guillaume, Aug 2026)
UK non-strategic → Aled; Europe → Lisa; Italy → Aled; European-owned UK
contractors (BAM, BESIX, Strabag, VolkerWessels, Jan de Nul, DEME) → Lisa.
Tier 1 of the resolver: live HubSpot company ownership overrides geography.
US: state → White Cap-team AE via `routing.us_state_ae` (Lawson Pacific,
Alicia Mountain, Ben Plains/TX/HI, Britain Midwest/South/DC, Brady
Mid-Atlantic/New England — the "new American guidelines", Aug 2026). Canada →
Justin, national. APAC → Jeremy. Alex/Jamie (SDR squad) are Region-filtered
in `triage.lists`, not AE-assigned — they work the same US regions from the
contractor side. Dan (US Data Hub) is out of the automated system entirely —
see the note on `triage.lists` above.

## Statuses (Notion select; drive the future map colours)
New (grey) → This week (blue) → Active Contact (amber, renamed live from
"Working on" — code and Notion must be kept in sync by hand, ensure_schema()
only adds missing properties, never renames existing options) / Recontact
later (hollow amber, has date) → On the project (green) / Disqualified /
Lost (both red — kept separate for partner conversations, never delete rows).

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
