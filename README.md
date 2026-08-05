# 007 — Project Radar (Converge)

Finds construction projects (tenders + news), qualifies them with AI, stores
them as structured rows in a Notion database, and runs a Monday triage that
gives each region its projects for the week. Deep research and HubSpot deals
happen on demand via Slack ✅ reactions. Runs entirely on GitHub Actions.

**Read `CLAUDE.md` first** — it holds the architecture, the load-bearing
rules (and why they exist), all confirmed IDs, and the roadmap.

## The pipeline
```
 TED (EU) ────┐
 FTS (UK) ────┤   filter + dedup      cheap qualify        Notion DB
 AusTender ───┼─▶ (Notion is the  ─▶ (GC, type, phase, ─▶ "007 Projects"
 SAM.gov (US)─┤    dedup authority)   fit + reason,        one row per
 News RSS ────┘                       location, geocode)   project, Status=New
                                                              │
                              Monday triage: top-N per region ▼
                     Slack list per region (manual intel first) ─▶ AEs work them
                                                              │
                     on demand: enrich.py (research pack) ✚ HubSpot deal
                                contacts.py (Amplemarket buying group)
```

## Workflows (GitHub Actions, UTC)
| Workflow | Schedule | Does |
|---|---|---|
| Ingest projects | weekdays 02:00 | Build the database silently |
| Monday triage | Mon 06:00 | Top-N per region → "This week" + Slack lists |
| Approvals | weekdays 09/12/15/17 | ✅ reactions → deals → research → contacts |
| Run step manually | dispatch | research / contacts on any deal or title |
| Tender digest / News radar | dispatch only | legacy Slack-card flows, retired |

## Key commands
```
python ingest.py --dry-run --days-back 7        # preview a week's intake
python ingest.py --historical --days-back 365 --max-rows 300   # backfill pass
python triage.py --dry-run                      # preview Monday's picks
python enrich.py --title "X" --country BE       # research any project
python contacts.py --deal-id 123                # buying group for a deal
```
Backfill is self-continuing: repeat the historical run until it writes fewer
rows than the cap. News RSS cannot backfill (feeds are shallow).

## Setup
Secrets (exact names): `SLACK_BOT_TOKEN` (scopes: chat:write, reactions:read,
reactions:write — **never** channels:history), `HUBSPOT_TOKEN`,
`ANTHROPIC_API_KEY`, `NOTION_TOKEN`, `AMPLEMARKET_TOKEN`, `SAM_API_KEY`
(optional). Repo Settings → Actions → Workflow permissions → Read and write.
The Notion parent page must be Connected to the "007 Data Storage"
integration. Full parameter reference lives in Notion:
"007 — Parameter Reference".

## Editing
All tuning is config, no code: `config.yaml` (routing, filters, triage
counts, models), `watchlist.yaml` (monitored contractors), `feeds.yaml`
(trade press), `skills/*/SKILL.md` (research frameworks). The `state/`
folder is runtime memory (dedup, posted cards, geocache) — never overwrite
it with a copy from elsewhere.
