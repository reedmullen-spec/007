"""Slack client for 007.

GitHub Actions is outbound-only, so approvals work by emoji reaction:
every card the bot posts is recorded in state (channel + ts + metadata);
the approvals job later asks reactions.get for each outstanding card and
acts when your white_check_mark appears.

Bot token scopes: chat:write, reactions:read, reactions:write. The bot
never reads channel history — approvals check reactions on its own posted
cards (tracked in state) via reactions.get.
"""
from __future__ import annotations

import json
import time

import requests

BASE = "https://slack.com/api"

META_PREFIX = "007meta:"
APPROVE_EMOJI = "white_check_mark"   # you react with this
DONE_EMOJI = "checkered_flag"        # the bot stamps this when the deal exists


class SlackClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _call(self, method: str, **kwargs) -> dict:
        resp = self.session.post(f"{BASE}/{method}", timeout=30, **kwargs)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
        return data

    # ------------------------------------------------------------ posting
    def post_parent(self, channel: str, text: str) -> str:
        """Post the weekly digest parent message; cards go in its thread."""
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
            {"type": "context", "elements": [{
                "type": "mrkdwn",
                "text": f"Project cards are in this thread — react "
                        f":{APPROVE_EMOJI}: on a card to create the HubSpot "
                        f"deal · `{META_PREFIX}{{\"wp\":1}}`",
            }]},
        ]
        data = self._call("chat.postMessage",
                          json={"channel": channel, "text": text,
                                "blocks": blocks, "unfurl_links": False})
        return data["ts"]

    def post_card(self, channel: str, header: str, lines: list[str],
                  meta: dict, link: str = "", mention: str | list[str] = "",
                  thread_ts: str | None = None) -> str:
        """Post a Block Kit card; returns the message ts."""
        body_text = "\n".join(lines)
        mentions = [mention] if isinstance(mention, str) else list(mention)
        mentions = [m for m in mentions if m and not m.startswith("TODO")]
        if mentions:
            body_text += "\nFor: " + " ".join(f"<@{m}>" for m in mentions)
        blocks = [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*{header}*\n{body_text}"}},
        ]
        if link:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<{link}|Open source notice / article>"},
            })
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"React :{APPROVE_EMOJI}: to create the HubSpot deal · "
                        f"`{META_PREFIX}{json.dumps(meta, separators=(',', ':'))}`",
            }],
        })
        payload = {"channel": channel, "text": header, "blocks": blocks,
                   "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        data = self._call("chat.postMessage", json=payload)
        return data["ts"]

    def reply_in_thread(self, channel: str, ts: str, text: str) -> None:
        self._call("chat.postMessage",
                   json={"channel": channel, "thread_ts": ts, "text": text,
                         "unfurl_links": False})

    def add_reaction(self, channel: str, ts: str, emoji: str = DONE_EMOJI) -> None:
        try:
            self._call("reactions.add",
                       json={"channel": channel, "timestamp": ts, "name": emoji})
        except RuntimeError as exc:
            if "already_reacted" not in str(exc):
                raise

    # ------------------------------------------------------- approvals
    def approval_on(self, channel: str, ts: str, approver: str) -> bool:
        """True if the approver has ✅'d this specific bot message and it
        isn't 🏁-stamped yet. Uses reactions.get (reactions:read scope) on
        the bot's OWN messages only — no channel history is ever read."""
        data = self._call("reactions.get",
                          params={"channel": channel, "timestamp": ts,
                                  "full": True})
        msg = data.get("message", {}) or {}
        reactions = {r["name"]: r for r in msg.get("reactions", [])}
        if DONE_EMOJI in reactions:
            return False
        approve = reactions.get(APPROVE_EMOJI)
        if not approve:
            return False
        if approver and approver not in approve.get("users", []):
            return False
        return True

    @staticmethod
    def _extract_meta(msg: dict) -> dict | None:
        for block in msg.get("blocks", []) or []:
            if block.get("type") != "context":
                continue
            for el in block.get("elements", []) or []:
                text = el.get("text", "")
                idx = text.find(META_PREFIX)
                if idx == -1:
                    continue
                raw = text[idx + len(META_PREFIX):].strip().strip("`")
                # metadata is the last backticked token on the line
                raw = raw.split("`")[0]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
        return None
