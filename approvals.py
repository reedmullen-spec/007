"""007 approvals — turn your ✅ reactions into HubSpot deals.

Checks the cards 007 itself has posted (tracked in state/cards.json) for
your :white_check_mark: reaction, via reactions.get on each outstanding
card — the bot never reads channel history or any human messages. For each
approved card:
  1. Re-checks HubSpot on tender_notice_id (CRM is the source of truth).
  2. Creates the deal in Sales Pipeline / Identified, owner = resolved AE.
  3. Replies in-thread with the deal link and stamps the card 🏁.

The 🏁 stamp — not the state file — is the guard against double-creation.
"""
from __future__ import annotations

import sys

from src import state
from src.config import env, load_config
from src.deal_naming import build_deal_name, split_deal_name
from src.framework import resolve_framework
from src.hubspot_client import HubSpotClient
from src.slack_client import SlackClient


def main() -> int:
    cfg = load_config()
    slack = SlackClient(env("SLACK_BOT_TOKEN"))
    hubspot = HubSpotClient(env("HUBSPOT_TOKEN"), cfg)

    # One-off bootstrap: make sure the custom properties exist.
    hubspot.ensure_notice_property()
    hubspot.ensure_summary_property()

    approver = cfg["slack"].get("approver_user_id", "")
    if approver.startswith("TODO"):
        approver = ""  # accept anyone's ✅ until configured

    created_log = state.load("created")
    cards = state.load("cards")
    created = 0

    import os
    import time as _time
    from enrich import enrich_deal
    from contacts import build_buying_group

    phase2_ready = bool(os.environ.get("ANTHROPIC_API_KEY")) and bool(os.environ.get("NOTION_TOKEN"))
    max_age = 14 * 86400   # stop checking cards older than two weeks

    if True:
        for key, card in list(cards.items()):
            if card.get("done"):
                continue
            if _time.time() - card.get("posted", 0) > max_age:
                card["done"] = True
                card["expired"] = True
                continue
            channel, ts, meta = card["channel"], card["ts"], card["meta"]
            try:
                if not slack.approval_on(channel, ts, approver):
                    continue
            except Exception as exc:
                print(f"WARNING: reaction check failed for {key}: {exc}",
                      file=sys.stderr)
                continue
            notice_id = meta.get("nid", "")
            title = meta.get("t", "(untitled)")
            ae = meta.get("ae")
            checkpoint = meta.get("cp", 1)

            try:
                if checkpoint == 2:
                    # ── CHECKPOINT 2: build the Amplemarket buying group ──
                    company, project = split_deal_name(title) if "—" in title else (title, title)
                    # Prefer the framework the pack was researched with,
                    # stamped on the card by enrich_deal. Older cards predate
                    # "fw" — fall back to the gate, which without a Project
                    # stage lands on the default, matching what checkpoint 1
                    # would have used anyway (rule 20).
                    framework = meta.get("fw") or resolve_framework(
                        cfg, country=meta.get("country", ""), text=title)
                    result = build_buying_group(
                        cfg, company=company, project=project,
                        framework=framework, country=meta.get("country", ""),
                        hubspot=hubspot, deal_id=meta.get("deal"))
                    slack.reply_in_thread(
                        channel, ts,
                        f"Buying group created in Amplemarket: "
                        f"{result.get('url', result.get('id'))}")
                    slack.add_reaction(channel, ts)
                    card["done"] = True
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
                    card["done"] = True
                    continue

                gc = meta.get("gc", "")
                deal_name = build_deal_name(title, contractor=gc,
                                            location=meta.get("country", ""))
                deal = hubspot.create_deal(name=deal_name, notice_id=notice_id, ae=ae)
                rename_hint = ("" if gc else
                              "\nRename to add the contractor once it's resolved "
                              "(`[Contractor] — [Project] — [Location]`).")
                slack.reply_in_thread(
                    channel, ts,
                    f"Deal created at Identified (owner: {ae}): {deal['portal_url']}"
                    f"{rename_hint}")
                slack.add_reaction(channel, ts)
                card["done"] = True
                created_log[meta.get("k", notice_id)] = {"deal_id": deal.get("id")}
                created += 1

                if phase2_ready:
                    # Step 2 fires straight off the back of deal creation and
                    # ends with the checkpoint-2 card.
                    enrich_deal(cfg, hubspot, deal_id=deal["id"], deal_name=deal_name,
                                notice_id=notice_id, ae=ae,
                                country=meta.get("country", ""), slack=slack)
                else:
                    print("Phase 2 secrets missing — skipping research step.")
            except Exception as exc:
                print(f"WARNING: approval failed for {notice_id}: {exc}", file=sys.stderr)

    state.save("created", created_log)
    state.save("cards", cards)
    print(f"Created {created} deals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
