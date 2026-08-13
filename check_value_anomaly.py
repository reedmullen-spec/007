"""Read-only: inspects the actual stored Value/Source for named rows, to
diagnose why megaprojects are scoring as "value below floor".

Usage:
    python check_value_anomaly.py "JFK Terminal 6" "Hudson Tunnel Project"
"""
from __future__ import annotations

import sys

from src.config import env, load_config
from src.notion_client import NotionClient


def main() -> int:
    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)
    names = sys.argv[1:]

    for name in names:
        rows = notion.query_rows({
            "property": cfg["notion"]["title_property"],
            "title": {"contains": name}})
        for row in rows:
            props = row.get("properties") or {}
            title = "".join(t.get("plain_text", "") for t in
                            (props.get("Name") or {}).get("title", []))
            value = (props.get("Value") or {}).get("number")
            source = ((props.get("Source") or {}).get("select") or {}).get("name", "")
            notice_id_prop = cfg["notion"]["notice_id_property"]
            nid_vals = (props.get(notice_id_prop) or {}).get("rich_text", [])
            notice_id = nid_vals[0].get("plain_text", "") if nid_vals else ""
            announced = ((props.get("Announced") or {}).get("date") or {}).get("start", "")
            print(f"{title[:70]:70s} Value={value!r} Source={source} "
                  f"notice_id={notice_id[:40]!r} announced={announced}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
