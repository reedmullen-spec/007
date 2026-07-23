# 007 — Tender & News Radar

Scans new UK/EU construction tenders (TED Europa + Find a Tender) and
project/contractor news (RSS), filters for concrete-relevant opportunities,
and posts cards to Slack routed to the right AE. You react ✅ to the ones
worth pursuing; a second job creates the HubSpot deal at *Identified* with
the notice ID stamped for permanent dedup.

```
 TED (EU) ──┐
 FTS (UK) ──┼─▶ filter: CPV · geo · keyword · value ─▶ HubSpot pre-check ─▶ Slack card (+AE)
 RSS news ──┘                                          (tender_notice_id)        │  you react ✅
                                                                          ┌──────▼───────┐
                                                                          │ HubSpot deal │
                                                                          │ @ Identified │
                                                                          │ owner = AE   │
                                                                          └──────────────┘
```

Runs entirely on **GitHub Actions** — free, no server. Three scheduled jobs:

| Workflow        | When (UTC, weekdays)   | Does |
|-----------------|------------------------|------|
| `digest.yml`    | 06:30                  | Fetch + filter tenders, post cards |
| `news.yml`      | 07,10,13,16,19:00      | Fetch + gate news feeds, post cards |
| `approvals.yml` | 09,12,15,17:00         | Read ✅ reactions, create deals, stamp 🏁 |

Baked-in confirmed values: Sales Pipeline `21257366`, Identified stage
`1326060402`, portal `2061231`, owners Reed `90628877` / Lisa `465940403` /
Aled `146637928` / Avi `32656681`.

---

## Setup (~15 minutes, secrets already done)

### 1. Create the repo and upload the files
1. github.com → **New repository** → name it (e.g. `007-radar`) → **Private** → Create.
2. Upload everything in this folder keeping the structure (easiest:
   **Add file → Upload files**, drag the whole unzipped folder contents in).
   Make sure `.github/workflows/` uploads — if you drag-and-drop and the
   hidden `.github` folder is skipped, create the three workflow files
   manually via **Add file → Create new file** with paths like
   `.github/workflows/digest.yml`.

### 2. Check your secret names
Repo → **Settings → Secrets and variables → Actions**. The workflows expect
exactly these names:

| Secret name       | Value |
|-------------------|-------|
| `SLACK_BOT_TOKEN` | your `xoxb-…` bot token |
| `HUBSPOT_TOKEN`   | your HubSpot private-app token |

If yours are named differently, either rename the secrets or edit the
`env:` blocks in the workflow files. (Your Amplemarket key isn't used in
this phase — see *Phase 2* below.)

### 3. Give Actions write permission (needed to commit state)
Repo → **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save. Without this, the
"Commit state" step fails.

### 4. Slack app scopes + channel
Your Slack app needs these **Bot Token Scopes** (OAuth & Permissions):
`chat:write`, `reactions:read`, `reactions:write`, `channels:history`
(add `groups:history` if using a private channel). Reinstall the app if you
add scopes.

Then invite the bot to your channel(s): `/invite @YourApp`.

### 5. HubSpot private app scopes
Settings → Integrations → Private Apps → your app → Scopes:
- `crm.objects.deals.read` + `crm.objects.deals.write`
- `crm.schemas.deals.write` — lets the bot auto-create the
  `tender_notice_id` custom deal property on first run. If you'd rather not
  grant it, create the property manually: Settings → Properties → Deal
  properties → Create property → single-line text, internal name
  **tender_notice_id**.
- `crm.objects.companies.read` — for the tier-1 AE ownership lookup.

### 6. Fill in `config.yaml`
Three TODOs:
- `slack.tender_channel_id` — channel ID (channel name → View channel
  details → ID at the bottom, starts with `C`).
- `slack.news_channels` — can all be the same ID as above to start.
- `slack.approver_user_id` — your Slack member ID (profile → ⋯ → Copy
  member ID, starts with `U`), so only *your* ✅ counts.

Optional: paste Lisa/Aled/Avi's Slack member IDs into `ae_slack_ids` to get
them @-mentioned on their cards.

### 7. Test
Actions tab → **Tender digest** → *Run workflow* → tick **Dry run**. Check
the log output looks sensible (matches printed, nothing posted). Repeat for
**News radar**. Then run both live once and check Slack. Tune
`config.yaml` keywords/thresholds until the noise level is right.

---

## Daily flow

1. Cards drop into Slack through the day — tenders in the morning, news
   through the day, each tagged with the suggested AE.
2. React **✅** on anything worth a deal.
3. Within a few hours the approvals job creates it in HubSpot at
   *Identified* (owner = the resolved AE), replies in-thread with the deal
   link, and stamps the card **🏁**. The 🏁 — not the state file — is the
   guard against double-creation, and the `tender_notice_id` search makes
   HubSpot itself the dedup source of truth.
4. Rename the deal to `[Contractor] — [Project]` once you've resolved the
   contractor (Reed Method checkpoint), and run your research pack.

## Tuning
- Too much noise → tighten `include_keywords`, raise `min_value_*`, or set
  `keep_unknown_value: false`.
- Missing things → add CPV prefixes or keywords; add targeted Google News
  feeds in `feeds.yaml` (they bypass the keyword gate).
- Routing wrong → adjust `routing` in `config.yaml`; tier 1 always defers
  to live HubSpot company ownership when a match is found.

## Phase 2 (not yet wired — needs extra secrets)
The two-checkpoint Reed Method flow (approval → deal → **automated research
pack into Notion → second ✅ → 15–20-contact Amplemarket buying group**)
needs `ANTHROPIC_API_KEY` (to run the ConcreteDNA / FieldAtlas SKILL.md
enrichment) and `NOTION_TOKEN`. The Amplemarket secret you've already added
will be used at that stage (`POST /lead-lists`, Bearer auth). Belgium/Hakron
deals get the pack but skip contact-building. Ask Claude to build phase 2
onto this repo when you're ready.
