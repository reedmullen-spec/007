"""Slack client for 007.

GitHub Actions is outbound-only, so approvals work by emoji reaction:
cards are posted with a hidden metadata line; the approvals job later reads
channel history, finds bot messages with your white_check_mark reaction that
aren't yet stamped with a checkered flag, and acts on the embedded metadata.

Bot token scopes: chat:write, reactions:read, reactions:write,
channels:history (plus groups:history for private channels).
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
    def post_card(self, channel: str, header: str, lines: list[str],
                  meta: dict, link: str = "", mention: str | list[str] = "") -> str:
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
        data = self._call(
            "chat.postMessage",
            json={"channel": channel, "text": header, "blocks": blocks,
                  "unfurl_links": False, "unfurl_media": False},
        )
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

    # ------------------------------------------------------------ polling
    def iter_approvals(self, channel: str, approver: str, days_back: int = 7):
        """Yield (ts, meta) for bot cards the approver ✅'d but aren't 🏁 yet."""
        oldest = str(time.time() - days_back * 86400)
        cursor = None
        while True:
            params = {"channel": channel, "oldest": oldest, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._call("conversations.history", params=params)

            for msg in data.get("messages", []):
                meta = self._extract_meta(msg)
                if meta is None:
                    continue
                reactions = {r["name"]: r for r in msg.get("reactions", [])}
                if DONE_EMOJI in reactions:
                    continue
                approve = reactions.get(APPROVE_EMOJI)
                if not approve:
                    continue
                if approver and approver not in approve.get("users", []):
                    continue
                yield msg["ts"], meta

            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

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
