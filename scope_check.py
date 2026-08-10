"""Read-only: scopes the Value-in-millions issue across the whole database."""
from src.config import env, load_config
from src.notion_client import NotionClient

cfg = load_config()
notion = NotionClient(env("NOTION_TOKEN"), cfg)
rows = notion.query_all_rows({})
print(f"{len(rows)} total rows")

suspects = []
for r in rows:
    props = r.get("properties") or {}
    value = (props.get("Value") or {}).get("number")
    source = ((props.get("Source") or {}).get("select") or {}).get("name", "")
    nid_prop = cfg["notion"]["notice_id_property"]
    nid_vals = (props.get(nid_prop) or {}).get("rich_text", [])
    notice_id = nid_vals[0].get("plain_text", "") if nid_vals else ""
    if value is not None and 0 < value < 100000 and not notice_id:
        title = "".join(t.get("plain_text", "") for t in (props.get("Name") or {}).get("title", []))
        suspects.append((title, value, source))

print(f"\n{len(suspects)} rows with small Value + blank notice_id (suspected millions-units bug):")
from collections import Counter
print("By source:", Counter(s[2] for s in suspects))
for t, v, s in suspects[:50]:
    print(f"  {t[:60]:60s} {v} {s}")
