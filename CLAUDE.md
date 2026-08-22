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
  skips politely without it) + CanadaBuys (Canada federal tenders;
  `src/sources/canada.py` — a static, unauthenticated CSV refreshed every 2
  hours, not a live-query API like the others, so no historical/pagination
  mode; needs a browser-like User-Agent or canadabuys.canada.ca 403s the
  default `requests` one) + news RSS (trade press + ~329 generated Google
  News contractor queries from `watchlist.yaml`; all of them keyword-gated
  — see rule 21, and rule 22 for where the contractor list comes from). CanadaBuys' tender
  notices carry no disclosed estimated value at all (verified live) — value
  only appears once awarded, via `recheck_awards.py`'s Canada reconciliation.
  Filter → dedup → qualify
  (one small Anthropic API call per candidate: summary, canonical GC,
  project type, work nature, project stage, expected concrete start +
  completion, location, use cases — observations only, no fit; see
  src/scoring.py) → geocode (Nominatim, cached in state) → score → create
  Notion row, Status=New. Also sweeps rows humans added by hand (no Notice
  ID): if a `Notice URL` is pasted, fetches the article text
  (`src/fetch_content.py`) so qualify works from real content instead of a
  bare title, and derives the title itself when the human only pasted a
  URL — then qualifies + scores and stamps Source=MANUAL (shared
  `src/manual_entry.py` pipeline, also used by `bulk_import.py` and
  `src/intake.py`). Also sweeps the separate "007 — Submit a project"
  intake database the same way (`src/intake.py`, `Source=INTAKE`,
  `notice_id` prefix `INTAKE:`) — see the intake note under Architecture.
  `bulk_import.py` (researched CSV lists, e.g. Jeremy's Queensland set)
  skips the Anthropic `qualify()` call entirely for a row that already
  supplies `project_type`/`work_nature`/`stage` columns — a researcher who
  already determined those doesn't need an API call to re-derive what
  they already know, and validates against the same canonical option
  lists (`src/qualify.py`'s `normalize_observations`) so a typo can't
  write a bad value into a Notion select property. Also derives
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
  rows persist and consume slots; "Recontact later" rows do NOT carry over
  (Aug 2026: `Recontact date`/`Next action`/`Next action date` were
  removed — see rule 15) — a person moves one back to "New" or "This
  week" by hand when it's worth revisiting. Flips picks to "This week", posts one
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
  `Outcome` / `Correction needed`. If a person edits one of the mirrored
  fact fields directly, that edit is authoritative — pushed to the master
  and no longer overwritten by later mirrors (see `sync.py` / rule #11).
- `sync.py` — weekdays 23:00 UTC. Pulls `Status` / `Notes` / `Outcome` /
  `Correction needed` from each AE page back to the master, always (the
  master never writes any of these itself, so there's nothing to fight
  over). `Next action` / `Next action date` / `Recontact date` were
  removed (Aug 2026) — see rule 15; "Recontact later" no longer carries a
  date anywhere. Non-empty `Correction needed`
  entries are ALSO DM'd to Reed once each (`state/sync.json` tracks what's
  already been flagged) — written to the master for the record, but still
  surfaced directly since it usually means some other field needs a human's
  attention. Separately, `sync_fact_drift()` (`src/ae_pages.py`) pushes an
  AE's direct edit to a mirrored fact field (`Fit`/`GC`/`Location`/`Expected
  concrete start`/`Expected completion`/`Value band`) back to the master
  immediately, detected against `state/ae_fact_snapshots.json` (what was
  last mirrored) — see rule #11.
- `recheck_awards.py` — weekly (Wednesday 04:00 UTC). Most TED/FTS rows
  have no `General contractor/JV` because they're pre-award tender notices —
  the winner genuinely isn't known yet (confirmed live: TED ~1% GC
  capture, FTS ~0%, vs NEWS ~67% since "awarded"/"breaking ground" stories
  inherently name one). This backfills existing rows past their `Tender
  deadline` once an award notice has since appeared. TED: buyer-name
  exact match (a buyer's name doesn't change between their tender and its
  award) + title-token similarity to pick the right award among that
  buyer's others — a fuzzy match, flagged in `Fit reason`, `Verified`
  stays False. FTS: exact OCID match (an award-stage OCDS release shares
  its OCID with the original tender release) — `Verified` set True.
  CANADA: exact `referenceNumber`/`solicitationNumber` match against the
  CanadaBuys contract history CSV (same field names as the tender-notices
  file) — also exact, `Verified` set True. Also backfills `Value`, since
  it's only disclosed post-award there — except when the award's value is
  literally `0`, which means undisclosed (verified live), not a genuine
  free contract, so it's left blank rather than written as real data.
- `enrich.py` — deep research (step 2). Standalone: `--deal-id` / `--title` /
  `--notice-id`. Anthropic API + web search, system prompt = the SKILL.md
  for the framework (concretedna for every AE except Avi, who gets
  fieldatlas — `enrichment.framework_by_ae`). Writes
  pack to Notion, pins a HubSpot note on the deal, posts checkpoint-2 Slack
  card. Packs must start with a 5-bullet TL;DR, max 600 words (AE feedback).
  That note carries the TL;DR itself, rendered to HTML by
  `src/note_body.py` (bullets as a real `<ul>`, inline bold/code/links
  honoured, same grammar as the Notion pack renderer), under the same
  `007 research pack: <link>` lead line as before — a link alone is one
  click too many in a deal timeline, and `tender_summary` doesn't show
  there at all. It stamps `SUMMARY_NOTE_MARKER`; see the `actions.py` note.
- `contacts.py` — step 3, Amplemarket buying group (15–20, persona titles by
  framework). Belgium refuses by design (see Hakron). Enginy may replace
  Amplemarket later — the search + lead-list creation is the swap point,
  don't extend that part. When called with a HubSpot deal (`hubspot` +
  `deal_id`, always true via the `Build contacts` checkbox and the
  checkpoint-2 Slack path; optional on the bare `--company` CLI build),
  every matched person is ALSO pushed into HubSpot as a contact
  (`HubSpotClient.upsert_contact`, deduped on email when known) and
  associated to that deal (`associate_default`, HubSpot's v4
  default-association endpoint — no hardcoded association type ID). This
  push logic sits after the Amplemarket-specific call, deliberately, so an
  Enginy swap only touches the search/list-creation half.
- `approvals.py` — several times daily. Processes ✅ reactions on cards
  (the digest.py/news.py legacy card flow only — see rule #13).
- `actions.py` — polls every 20 minutes (plus `workflow_dispatch` for an
  immediate on-demand run). Self-serve alternative for
  ingest.py-sourced rows, which never get a Slack card: three checkboxes on
  the master row — `Enrich`, `Create deal`, `Build contacts` — independent
  of each other and of Status, tickable at any time in any combination.
  `Enrich` creates the HubSpot deal first if none exists yet (the pack needs
  one to pin its note to, same as `enrich.py --title`); `Create deal` reuses
  `find_deal_by_notice_id` for the same dedup guarantee as every other path
  (rule #2) and is a no-op if `Enrich` already created one in the same run.
  `Create deal` also posts the row's summary onto the deal as a pinned note
  (`Enrichment summary` when the pack has run, else the ingest `Summary`) —
  the `tender_summary` property alone is invisible in the timeline, which is
  where reps actually read. Idempotent on `SUMMARY_NOTE_MARKER`
  (`007 project summary`) found in the deal's existing notes rather than on
  "did this run create the deal", so a run that creates the deal and then
  fails on the note still writes it on retry. `enrich_deal()` stamps the same
  marker, which is what makes both boxes on one row produce ONE summary note
  and not two: `Enrich` goes first and its note carries the freshly extracted
  TL;DR, so `Create deal` stands down rather than falling back to the stale
  ingest `Summary` on its pre-run snapshot of the row. Ticked days apart the
  other way round you do get both, correctly — the notice note, then the
  richer pack note. Needs `crm.objects.notes.read` on the private app for the
  check; without it the note is written only on the run that created the deal
  (never duplicated, but a failed retry loses it).
  `Build contacts` needs `General contractor/JV` filled in AND a HubSpot deal
  already existing (checked via `find_deal_by_notice_id` — tick
  `Enrich`/`Create deal` first, or it must predate this row), and honours
  the Belgium/Hakron skip (rule #3) as a silent no-op, not a retry-forever
  failure. Never actually a same-run ordering problem: `ACTIONS`' fixed
  order (Enrich, Create deal, Build contacts) always processes `Build
  contacts` last, so a deal created by either of the other two boxes on
  the same row in the same run already exists by the time it fires. A box
  that succeeds is unchecked (so it doesn't re-fire); a box that fails is
  left checked (so the next run retries it) — same guard philosophy as the
  🏁 stamp in approvals.py.
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
11. **AE-owned fields sync one way, fact fields the other — except when a
    person edits one.** `triage.py` writes facts to a person's page every
    Monday but never touches `Status`/`Notes`/`Outcome`/`Correction
    needed` on an existing row. `sync.py` writes all of those back to
    the master, always (the master
    never writes them itself, so no fight). Fact fields (`Fit`/`GC`/
    `Location`/`Expected concrete start`/`Expected completion`/`Value
    band`) are the one case that CAN go either way: normally master ->
    AE page only, but if a person edits one directly, `sync_fact_drift()`
    (`src/ae_pages.py`) detects it against `state/ae_fact_snapshots.json`
    (a snapshot of what was last mirrored — Notion has no per-property
    edit history, only page-level `last_edited_time`) and pushes it to the
    master immediately; future mirrors leave that field alone from then on
    since the snapshot now matches the person's value, not the old
    master's. This is a real distinction from a plain two-way sync: without
    the snapshot, there'd be no way to tell "person edited this" apart from
    "unchanged since we wrote it," and mirroring would either always win
    (silently discarding edits) or always lose (fighting the next rescore).
12. **The master "007 Projects" database is restricted to Reed + Issam.**
    Anyone else who needs to add a project uses the separate "007 —
    Submit a project" intake database (`ensure_intake_database`,
    `src/intake.py`) instead — its own page, its own Notion sharing, a
    deliberately minimal schema. A form view on the master itself was
    considered and rejected: the Notion API can't hide fields on a form,
    so it would expose every master field (including computed ones like
    `Fit`/`AE`/`SDR`) to whoever could submit it. Don't add a form view
    directly on the master database — route new manual-entry ideas
    through the intake database instead.
13. **Two separate trigger surfaces feed the same enrich/deal/contacts
    engine, and they don't talk to each other.** digest.py/news.py post
    Slack cards; a ✅ reaction is read by approvals.py, which creates the
    deal and (secrets permitting) chains straight into `enrich_deal()` then
    offers a checkpoint-2 card for `build_buying_group()`. ingest.py rows
    never get a card, so `actions.py` polls the master row's checkboxes
    instead and calls the identical `enrich_deal()`/`build_buying_group()`/
    `hubspot.create_deal()` functions directly — no Slack, no staging.
    Both paths still land on the same HubSpot dedup key, so a row touched by
    one is safely inert to the other. `cfg["hubspot"]["summary_property"]`
    (`tender_summary`) is the one piece of state either direction writes:
    `enrich_deal()` always backfills it from the pack's TL;DR, and
    `create_deal()` reads the Notion `Enrichment summary` property to seed
    it if research already ran — so it ends up correct regardless of which
    of Enrich/Create-deal a person ticks first. The same TL;DR also goes on
    as a note from whichever path gets there first (`src/note_body.py`),
    deduped on `SUMMARY_NOTE_MARKER`.

14. **`General contractor/JV` is one field, not two.** Consolidated
    (Aug 2026) from separate `General contractor` + `JV / parents`
    properties — both had drifted (JV/parents held richer manually-
    researched detail than the "canonical name" GC field was supposed
    to carry, and 32 rows disagreed between the two). `NotionClient.
    _gc_jv_text(gc, jv_parents)` is the one place that combines a
    contractor name with its JV parents into the stored string; every
    writer (ingest.py, recheck_awards.py, import_scored_csv.py) goes
    through it rather than formatting its own. Notion column deletion
    isn't auto-migrated (same as the "Active Contact" rename above) —
    the two old properties were dropped from the live database by
    hand, and `ensure_schema()`'s SCHEMA dict now only knows about the
    merged field, so a stale local checkout that still assumes
    `General contractor` exists will silently read blank, not error.
15. **`Next action` / `Next action date` / `Recontact date` were removed
    from the master (Aug 2026)** — Reed's call: follow-up/recontact
    tracking is meant to live in the HubSpot deal, not duplicated in
    Notion too. Note this is a stated intent, not yet a built integration
    — `src/hubspot_client.py` has no next-action/task property or
    reminder logic of its own as of this change; until that exists,
    there is genuinely no automated "what's due" surface anywhere.
    `sync.py` no longer pulls or writes any of the three, and
    `triage.py`'s carry-over query no longer has a `Recontact date` to
    filter on, so **"Recontact later" no longer auto-resurfaces**. A row
    sent there stays there until a human manually flips its Status back
    to "New" or "This week" — there is no automated path back anymore.
    Before removal, 70 rows had live `Next action` text; that text was
    archived into each row's `Notes` field (prefixed `Next action
    (archived): ...`) so it wasn't silently destroyed — check `Notes`
    before assuming a row has no history. `Next action` / `Next action
    date` still exist as live, fillable fields on the 13 per-AE "My week"
    pages (`AE_PAGE_SCHEMA` was NOT changed there) — nothing writes them
    to the master anymore, so anything an AE types into those two fields
    on their own page now goes nowhere. If that turns out to matter,
    those per-AE fields need removing too; they were deliberately left
    alone here since dropping columns across 13 separate live databases
    wasn't part of what was asked.

16. **Dropping a live Notion column and pushing its code fix are not
    atomic — a cron tick in between resurrects it.** `ensure_schema()` is
    additive-only: it re-adds any property in the code's SCHEMA dict
    that's missing from the live database. `actions.py` runs every 20
    minutes (plus `ingest.py` at 02:00/13:00 UTC) and calls it. If you
    DROP COLUMN in Notion before the matching code change lands on
    `main`, the next scheduled run — using whatever is still on `main` —
    silently re-adds the column as empty, and any ingest that happens in
    that window writes real data into the old field name again. This
    happened during the Aug 2026 `General contractor`/`JV / parents`/
    `Next action`/`Next action date`/`Recontact date`/`Currency` cleanup:
    a stale-code `ingest.py` run resurrected all five removed properties
    and wrote 7 new rows into the old `General contractor` field, and
    separately ~19 rows of live manual research (Ontario Line packages,
    GO Expansion, PORR, Bechtel, Bouygues UK, Ashbridges Bay, Gardiner
    Expressway, Darlington SMR, Ottawa O-Train) got entered directly in
    Notion during the same window using the resurrected `JV / parents`/
    `Next action` fields, because they were visible in the Notion UI
    again. All of it was recovered by hand (merged into `General
    contractor/JV` and archived into `Notes`) — nothing was lost, but it
    required a second full reconciliation pass. **The fix**: push the
    code change and confirm it's on `main` BEFORE dropping the Notion
    column, not after — and re-check the live schema immediately before
    calling a column removal done, since a resurrection is silent.
17. **`Build contacts` re-firing is a no-op if `Contacts list` is already
    set** (`actions.py`'s `_run_contacts`, Aug 2026). `build_buying_group()`
    has no dedup of its own — every call searches Amplemarket and creates
    a brand-new lead list, full stop. The checkbox is the only guard
    against re-firing, and `actions.py` correctly unchecks it after every
    success, so the only way to get duplicates is a human re-ticking it.
    Real incident: three manually-researched rows (Bouygues UK, PORR,
    Bechtel) each got 4 duplicate Amplemarket lists from repeated ticks
    over one morning, on top of a legitimate hand-built list from the day
    before — 5 lists per project, only the last one ever linked from
    `Contacts list` (a plain overwrite, not an append) so the other 3 per
    project were silently orphaned. To force a genuine rebuild, clear
    `Contacts list` first, then tick the box — ticking it while a list
    already exists just prints a skip message and unchecks itself.
    This guard is Notion-checkbox-specific (`actions.py`); the bare CLI
    (`contacts.py --company X`, no deal) and `approvals.py`'s checkpoint-2
    path are untouched — the CLI is a deliberate one-off per its own
    docstring, and checkpoint-2 already has its own per-card guard
    (`card["done"]`, rule 13).
18. **`Value` must be raw dollars, never millions-shorthand** — `_disqualify()`
    (`src/scoring.py`) compares it directly against `disqualify_below_value`
    (5,000,000) with no unit conversion, and Value band thresholds
    (50M/250M) assume the same. Typing "400" to mean "€400m" reads as
    $400 against a $5M floor and kills the row. This has now happened
    twice: first for ~32 `Source=NEWS` rows (found via `scope_check.py`),
    then Aug 2026 for 46 `Source=MANUAL` hand-researched rows (Ontario
    Line, Darlington SMR, PORR, Bechtel, etc. — genuinely large projects,
    all wrongly disqualified). `fix_value_units.py` is now source-agnostic
    (originally NEWS-only) — run it, THEN `rescore_existing.py` (or a
    scoped equivalent) to re-derive Fit from the corrected Value; running
    rescore first just re-disqualifies everything against the still-wrong
    number. `Concrete opportunity` — "our prize, not the project size"
    (§1) — is a separate field entirely and never substitutes for a
    correct `Value`; don't try to soften the floor check against it
    instead of just fixing the data.
19. **`rescore_existing.py` used to only ever set `Status=Disqualified`,
    never revive it** — it wrote Status when a fresh score came back
    Disqualified, but did nothing when a rescore un-disqualified a row
    that already had Status=Disqualified from an earlier pass. Real
    incident (Aug 2026): 670 rows sat invisible in every AE's triage
    despite a legitimate, non-Disqualified `Fit` — 28 of them High. Fixed:
    the script now flips `Status` back to `New` when `old_status ==
    "Disqualified"` and the fresh score isn't — but ONLY then, and ONLY
    when the old `Fit reason` doesn't contain "Auto-disqualified" (that
    tag means `cleanup_low_value.py`/`cleanup_filters.py` disqualified it
    on a criterion — value floor, keyword filters — that `score_project()`
    never checks, so a clean rescore can't tell whether that separate
    reason still applies — `cleanup_news_filters.py` is a third such
    source, and the news gate is likewise invisible to `score_project()`). New `--include-disqualified` flag sweeps
    Status=Disqualified rows too, not just Status=New — run this
    periodically (not just after ingest) so a stale Disqualified Status
    doesn't silently outlive whatever originally caused it.

20. **The research framework is chosen from the PROJECT, not the AE, and the
    FieldAtlas gate is a deliberate AND.** Until Aug 2026 this was
    `enrichment.framework_by_ae` (Avi → fieldatlas, everyone else →
    concretedna). That never worked as intended: **`routing.py` never assigns
    Avi from geography** — he is in `ae_slack_ids`, `hubspot.owners`,
    `ae_sdr_map` and `triage.lists`, but in none of `country_ae_map`,
    `eu_default`, `us_state_ae`, or `country_ae_map_extra` — so the only
    routes to `AE=avi` were HubSpot company ownership (tier 1) or a human
    hand-tagging the row. An ingested UK modular prison resolved GB → aled →
    concretedna and got a concrete-sensing pack for a modular project.
    Replaced by `src/framework.py`'s `resolve_framework()`, which returns
    `fieldatlas` only when **all three** hold: `Country == GB`,
    `Project stage == "PCSA / preconstruction"`, and a word-boundary DfMA
    keyword hit on title + `Summary`. `framework_by_ae` is deleted, not
    deprecated — config.yaml is versioned with the code, so unlike a Notion
    column (rule 16) there is no stale-config window to manage.
    Four things about this that look arbitrary:
    - **AND, not OR** (Reed's call). With OR, every UK PCSA project — a
      highway widening at PCSA, say — would get a modular research pack and
      modular personas. The cost is real: verified against the live database
      (893 rows, Aug 2026) the gate matches **exactly one row** — Bouygues UK
      / Cambridge Children's Hospital, `AE=avi`, Fit High. That is not a bug,
      it is the only UK PCSA project in the database at all (31 rows are at
      PCSA; 30 of them are US/CA/AU). `explain()` prints which leg closed so
      a surprising concretedna choice is debuggable without reading the code.
      If the count needs to go up, dropping the PCSA leg is the lever — UK +
      structured-DfMA alone matches 3.
    - **`Region`, NOT `Country`.** `Country` is not normalised in the live
      database: it holds both `GB` (71 rows) and `United Kingdom` (4), and
      likewise `US`/`United States`, `CA`/`Canada`, `AU`/`Australia`,
      `DE`/`Germany`. `Region` is a canonical select, so it is the reliable
      test. **The first version of this gate matched `Country == "GB"`
      exactly and therefore matched zero rows — including the single row it
      was designed for, whose Country is `United Kingdom`.** Country is still
      accepted as a fallback for CLI paths with no row, normalised through
      `fieldatlas_gate.country_aliases`. IE is still excluded: Ireland is not
      the UK, even though `country_ae_map` sends both GB and IE to Aled.
    - **Avi's non-UK DfMA work now gets concretedna**, deliberately. This
      reverses the old FieldAtlas skill line about "a UK prison and a
      European gigafactory are both Avi" — that line was removed from
      `skills/fieldatlas/SKILL.md` because the gate makes it false.
    - **DfMA IS a structured field — two of them.** `Use case`
      (multi-select) has a canonical `DfMA / modular` option, set by
      `qualify()` at ingest, and `Fit profile` has `Industrialised
      construction / DfMA`, set by `scoring.py`. Either one satisfies the
      leg. An earlier version of this rule claimed no such field existed,
      having checked only `WORK_NATURES` (New build / Extension /
      Replacement / Widening / Refurbishment / Phase / Unknown — which
      genuinely has nothing) and stopped there; **do not repeat that, check
      `Use case` and `Fit profile` before concluding a concept is
      unmodelled.** `dfma_keywords` survives as a FALLBACK for rows whose
      `Use case` is Unknown or blank, word-boundary matched per rule 9
      (`commmodular` must not match `modular`; the MMC and DfMA acronyms make
      that mandatory, not cosmetic). **`precast` is deliberately NOT a
      keyword** — it appears in ordinary in-situ tenders constantly and would
      drag genuine ConcreteDNA projects onto the wrong framework. Keywords
      must not be the primary signal: the Cambridge row carries
      `DfMA / modular` in `Use case` while its title and Summary contain no
      DfMA keyword whatsoever, so a keyword-first gate rejected precisely the
      row it was built to catch. Because the fallback can still fire on a
      passing mention, the FieldAtlas skill is told the gate may have
      misfired and to say so in the Snapshot.
    **The framework picks two things, and they must agree:** the research
    system prompt (`enrich.py`) AND the Amplemarket persona titles
    (`contacts.py`, `amplemarket.titles`). A modular pack matched against
    concrete personas is the failure mode. `resolve_framework()` is
    deterministic from the row's fields precisely so both paths reach the same
    answer without storing it. `actions.py`'s `_gate_signals(notion, row)` is
    the single place those fields are read, and both paths pass its result
    straight through as `enrich_deal(gate=...)` / `resolve_framework(**...)`,
    so they cannot drift apart. On the Slack card path `enrich_deal()` instead
    stamps the resolved framework onto the checkpoint-2 card meta as `fw` and
    `approvals.py` reuses it, because card meta carries no `Project stage` and
    would otherwise re-derive a different answer. Paths with no Notion row
    (`enrich.py --title`, `contacts.py --company`) cannot see a stage or a
    `Use case`, so the gate can never match there and always returns
    concretedna — both grew `--framework` to force it, plus `--stage` and
    `--region` to supply the missing legs.

21. **Every news feed is keyword-gated, and three things about the news
    gate were measured on 2026-08-22, not guessed.** The whole set was
    re-derived from one live collection run (640 raw entries, 89 feeds);
    re-measure before reverting any of it.
    - **The watchlist queries' `AND` clause is advisory.** `watchlist.yaml`
      builds `"{entity}" AND ("awarded" OR "breaking ground" OR …)` and
      `watchlist_feeds()` used to set `keyword_gate: False` on the strength
      of it. Google News does not honour it: 4 of the 9 in-date items it
      returned carried none of the required phrases, including "Bouygues UK
      bosses say strategy is 'foundation for growth'". Those feeds are now
      gated like any other; the query only biases ranking. `when:7d` in the
      query is also ignored — tested, byte-identical results.
    - **Age-filter BEFORE `MAX_ITEMS_PER_FEED`, never after.** Google News
      search feeds are relevance-ranked, not chronological: 1568 entries
      across the 84 watchlist feeds, only 9 inside `max_age_days`, and two
      of those sat at positions 13 and 32 (a PCL housing award, a $2.4bn
      tram contract). Slicing the first 10 raw entries kept years-old items
      that `_too_old` then dropped anyway while discarding real awards.
    - **Match on the headline with the publisher stripped** (`_match_title`,
      via each entry's `source.title`). Google News appends " - Publisher"
      to every title, and it poisons the gate both ways: "BAM wins £55.9
      million Huddersfield museum contract … - Fit Out Awards 2026" — a real
      award — was killed by the `awards` ceremony exclusion matching the
      outlet name. The STORED title keeps the suffix on purpose, because
      `NewsItem.dedup_key` derives from it (rule 6) and rewriting it would
      reset every news dedup key.
    The gate carries bare verbs (`wins`, `secures`, `selected`, `lands`)
    because rigid phrases missed any award with words in the middle —
    "wins contract" does not match "BAM wins £55.9 million Huddersfield
    museum contract". They are looser than phrases by design, and are paid
    for by the legal (`trial`, `lawsuit`, `court`, …), awards-ceremony
    (`awards`, `shortlist`, `finalists`) and financials (`turnover`,
    `losses`, `profit`, …) exclusions. **Don't add a bare verb without the
    matching exclusions, or take an exclusion away without re-checking the
    verbs.** Net effect of the change, on that run's real headlines: 9 kept
    -> 13 kept, +6 genuine awards recovered, -5 fluff dropped (a
    slip-and-fall verdict, an awards shortlist, two sets of company
    financials, one corporate-strategy piece).
    **A news-gate change only affects future ingest** — for rows already in
    the master, `cleanup_news_filters.py` (dispatch-only, `--dry-run`
    defaulted ON in the workflow) is the retroactive path. It cannot share
    `cleanup_filters.py`, which gates TED/FTS/AUSTENDER/SAM through
    `filter_projects()` and skips NEWS on purpose, for two reasons: the news
    gate is word-boundary matched (`news._word_hit`) while
    `filtering._keyword_hit` is a plain substring test, and a stored NEWS
    title still carries its " - Publisher" suffix with no `source.title`
    left to strip it by. Its two checks are therefore deliberately
    asymmetric — an exclude hit counts only on the publisher-stripped
    title (so an outlet name can never disqualify a row, the "Fit Out
    Awards 2026" failure), and a gate miss counts only when NEITHER the
    full nor the stripped title hits the gate (so a bad strip can't
    disqualify a row that would pass). Don't "simplify" that to one check.

22. **The UK/Europe/US contractor list in `watchlist.yaml` is generated, and
    the generator is additive on purpose.** `data/gc_tiers_uk_eu_us_2026-08-22.csv`
    is the flattened Tier 1 & Tier 2 GC document (406 rows: 147 Tier 1, 259
    Tier 2, compiled 22 Aug 2026 from The Construction Index / Building Top
    150 / Barbour ABI for the UK, Deloitte GPoC + CE100 for Europe, and ENR
    2026 Top 400 ranks 1-200 for the US). `build_watchlist_from_gc_doc.py`
    merges it in and took the watchlist from 84 feeds to 329.
    - **It never rewrites or deletes an existing entry.** Several watchlist
      names are tuned queries, not legal company names — "Multiplex
      construction", "Careys construction", "RG Group construction", "NCC
      construction" carry a disambiguating word so the query doesn't drown
      in unrelated hits. The name is also stored as `NewsItem.entity` and
      seeds the `[Contractor] — …` HubSpot deal name. The script reports
      near-duplicates for a human to judge instead of merging them.
    - **Inclusion is Tier 1 + Tier 2 scored High concrete fit, minus
      anything scored Low.** Tier 2 High is where the self-perform frame and
      groundworks contractors sit (Careys, JRL, Byrne, J Coffey, O'Keefe,
      Garney) — the highest concrete-intensity buyers in the document. The
      17 Low-fit rows are steel-led, interiors, or utility/telecom (Cimolai,
      William Hare, JRM, MasTec, SOLV Energy); 80 Tier 2 Medium rows are
      also held back. Relax `included()` to widen.
    - **Locales are English everywhere** (`hl: en`), with `gl`/`ceid` still
      country-targeted. Measured across six major EU contractors: the
      local-language feeds returned 27 raw entries between them against 242
      for the identical English query, and only the English ones produced a
      gate pass. This follows from rule 21 — the gate keywords are English,
      so "VINCI remporte un contrat" can never pass. The 21 pre-existing
      local-language EU entries were switched to English for the same
      reason; today's gate change had silently muted them. **Don't set `hl`
      back to a local language without adding local-language gate
      keywords.**
    - **Punctuation in an entity name degrades the query.** "Byrne Group
      (Byrne Bros)" returns 0 entries where "Byrne Bros" returns 3;
      "Careys / Carey Group" returns 0 where "Carey Group" returns 9. So
      `clean_name()` drops parentheticals and cuts slash-alternatives to the
      first form. A Google News RSS 302 is just how the endpoint answers —
      it is NOT rate limiting: all 23 zero-entry 302s in a 330-feed sweep
      still returned 0 when retried individually after a cool-off.
    - Cost of the bigger list: ~0.47s per feed, so news collection went from
      ~33s to ~155s. Both `news.yml` and `ingest.yml` allow 30 minutes.
      `news.max_cards_per_run` (25) has not started binding — a live run on
      329 feeds produced 16 gated items, 9 after dedup.

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
- HubSpot private-app scopes are listed in `src/hubspot_client.py`'s
  docstring. `crm.objects.notes.read` is the newest one (summary-note dedup
  on `Create deal`) — write-only note access degrades that path silently
  rather than erroring, so check the app if notes stop appearing.
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
later (hollow amber — no longer has a date, see rule 15) → On the project
(green) / Disqualified / Lost (both red — kept separate for partner
conversations, never delete rows).

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
