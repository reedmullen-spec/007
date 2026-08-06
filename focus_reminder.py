"""Friday-evening reminder — prompts each triage.lists person to submit
their weekly focus before Sunday 20:00 UTC, at ~4pm THEIR local time
rather than one fixed UTC time for everyone.

Runs hourly every Friday (see focus-reminder.yml); each run checks every
person's current local hour via their configured IANA timezone
(triage.lists[].timezone) and only messages the ones for whom it's
currently ~4pm. Using zoneinfo per-person instead of hardcoding UTC hours
means this stays correct across DST changes automatically (e.g. Europe/
London resolves to BST or GMT depending on the date) — no seasonal cron
updates needed.

Usage:
    python focus_reminder.py                        # live, respects local-time check
    python focus_reminder.py --dry-run               # print who WOULD be messaged now
    python focus_reminder.py --dry-run --ignore-time  # preview everyone, any time
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from zoneinfo import ZoneInfo

from src.config import env, load_config
from src.notion_client import NotionClient
from src.slack_client import SlackClient

TARGET_LOCAL_HOUR = 16   # 4pm


def _next_monday(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return today + dt.timedelta(days=days_ahead)


def _is_local_4pm(tz_name: str, now_utc: dt.datetime) -> bool:
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return False
    return local.hour == TARGET_LOCAL_HOUR


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-time", action="store_true",
                        help="Skip the per-person local-time check (testing only)")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    ae_slack = cfg["slack"].get("ae_slack_ids", {})

    focus_db_id = notion.ensure_focus_database()
    focus_url = f"https://www.notion.so/{focus_db_id.replace('-', '')}"
    monday = _next_monday()
    now_utc = dt.datetime.now(dt.timezone.utc)

    text = (f":calendar: *Weekly focus — due Sunday 20:00 UTC*\n"
            f"Got a specific angle for next week (w/c {monday.strftime('%d %b %Y')})? "
            f"A region, project type, or client you want your list built around?\n\n"
            f"Add it here before Sunday evening: <{focus_url}|007 Weekly focus>\n"
            f"_No focus entered → Monday's list defaults to standard ranking "
            f"(manual intel first, then fit, then value)._")

    slack = None if args.dry_run else SlackClient(env("SLACK_BOT_TOKEN"))
    sent = 0
    for list_cfg in cfg["triage"]["lists"]:
        name = list_cfg["name"]
        tz_name = list_cfg.get("timezone", "UTC")
        if not args.ignore_time and not _is_local_4pm(tz_name, now_utc):
            continue
        member_id = ae_slack.get(name, "")
        if not member_id or member_id.startswith("TODO"):
            print(f"  skip {name}: no Slack ID configured")
            continue
        print(f"  {'would message' if args.dry_run else 'messaging'} {name} ({tz_name})")
        if not args.dry_run:
            slack._call("chat.postMessage",
                        json={"channel": member_id, "text": text,
                              "unfurl_links": False})
            time.sleep(1)
        sent += 1

    verb = "Would message" if args.dry_run else "Messaged"
    print(f"{verb} {sent} people")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
