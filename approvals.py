"""007 approvals — turn your ✅ reactions into HubSpot deals.

Scans the tender channel and news channels for bot cards you've reacted
:white_check_mark: to that aren't yet stamped :checkered_flag:. For each:
  1. Re-checks HubSpot on tender_notice_id (CRM is the source of truth).
  2. Creates the deal in Sales Pipeline / Identified, owner = resolved AE.
  3. Replies in-thread with the deal link and stamps the card 🏁.

The 🏁 stamp — not the state file — is the guard against double-creation.
"""
from __future__ import annotations

import sys

from src import state
from src.config import env, load_config
from src.hubspot_client import HubSpotClient
from src.slack_client import SlackClient


def main() -> int:
    cfg = load_config()
    slack = SlackClient(env("SLACK_BOT_TOKEN"))
    hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)

    # One-off bootstrap: make sure the custom property exists.
    hubspot.ensure_notice_property()

    channels = {cfg["slack"]["tender_channel_id"]}
    channels.update(cfg["slack"].get("news_channels", {}).values())
    channels = {c for c in channels if c and not c.startswith("TODO")}

    approver = cfg["slack"].get("approver_user_id", "")
    if approver.startswith("TODO"):
        approver = ""  # accept anyone's ✅ until configured

    created_log = state.load("created")
    created = 0

    import os
    from enrich import enrich_deal
    from contacts import build_buying_group

    phase2_ready = bool(os.environ.get("ANTHROPIC_API_KEY")) and bool(os.environ.get("NOTION_TOKEN"))

    for channel in channels:
        for ts, meta in slack.iter_approvals(channel, approver):
            notice_id = meta.get("nid", "")
            title = meta.get("t", "(untitled)")
            ae = meta.get("ae")
            checkpoint = meta.get("cp", 1)

            try:
                if checkpoint == 2:
                    # ── CHECKPOINT 2: build the Amplemarket buying group ──
                    company, project = title, title
                    if "—" in title:
                        company, project = [s.strip() for s in title.split("—", 1)]
                    framework = cfg["enrichment"]["framework_by_ae"].get(ae, "concretedna")
                    result = build_buying_group(
                        cfg, company=company, project=project,
                        framework=framework, country=meta.get("country", ""))
                    slack.reply_in_thread(
                        channel, ts,
                        f"Buying group created in Amplemarket: "
                        f"{result.get('url', result.get('id'))}")
                    slack.add_reaction(channel, ts)
                    created += 1
                    continue

                # ── CHECKPOINT 1: create the deal, then enrich ──
                existing = hubspot.find_deal_by_notice_id(notice_id)
                if existing:
                    slack.reply_in_thread(
                        channel, ts,
                        f"Already in HubSpot as “{existing['properties'].get('dealname')}” — "
                        f"no new deal created.")
                    slack.add_reaction(channel, ts)
                    continue

                deal = hubspot.create_deal(name=title, notice_id=notice_id, ae=ae)
                slack.reply_in_thread(
                    channel, ts,
                    f"Deal created at Identified (owner: {ae}): {deal['portal_url']}\n"
                    f"Rename to `[Contractor] — [Project]` once the contractor is resolved.")
                slack.add_reaction(channel, ts)
                created_log[meta.get("k", notice_id)] = {"deal_id": deal.get("id")}
                created += 1

                if phase2_ready:
                    # Step 2 fires straight off the back of deal creation and
                    # ends with the checkpoint-2 card.
                    enrich_deal(cfg, hubspot, deal_id=deal["id"], deal_name=title,
                                notice_id=notice_id, ae=ae,
                                country=meta.get("country", ""), slack=slack)
                else:
                    print("Phase 2 secrets missing — skipping research step.")
            except Exception as exc:
                print(f"WARNING: approval failed for {notice_id}: {exc}", file=sys.stderr)

    state.save("created", created_log)
    print(f"Created {created} deals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
