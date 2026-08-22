"""Retroactive cleanup for NEWS rows — the sibling `cleanup_filters.py`
deliberately doesn't touch them.

`cleanup_filters.py` re-checks TED/FTS/AUSTENDER/SAM rows against
`filter_projects()`. NEWS rows go through a completely separate gate
(`news.py`: `news.gate_keywords` / `news.exclude_keywords`, word-boundary
matched), so they had no retroactive path at all — a news-gate change only
ever affected future ingest, and the fluff already in the master stayed
there. That's what this fixes.

Two things make a stored NEWS row harder to re-check than a tender row:

  - **Matching must use news.py's `_word_hit`, not `filtering._keyword_hit`.**
    The latter is a plain substring test; the news gate has always been
    word-boundary matched (rule 9). Using the tender matcher here would
    disqualify rows the live gate keeps.
  - **The stored title still carries Google News' " - Publisher" suffix.**
    That's deliberate (`_match_title`'s docstring: `NewsItem.dedup_key`
    derives from the stored title, so it is never rewritten), but it means
    the publisher name is inside the text being matched, and it caused a
    real false positive when the exclusions landed: "BAM wins £55.9 million
    Huddersfield museum contract … - Fit Out Awards 2026" tripped the
    `awards` ceremony exclusion on the OUTLET, not the story. A stored row
    has no `source.title` to strip by, so `_strip_publisher()` guesses —
    and because it guesses, the two checks below are deliberately asymmetric.

Erring toward not touching real data, same as `cleanup_filters.py`:

  - an EXCLUDE hit only counts on the publisher-stripped title, so a
    publisher name can never disqualify a row on its own
  - a GATE MISS only counts when NEITHER the full nor the stripped title
    hits the gate, so a bad strip can't disqualify a row that would pass

Only `Status=New` rows are touched — a gate change shouldn't retroactively
kill something an AE is already working (same rule as both siblings). The
`Auto-disqualified` tag in `Fit reason` is load-bearing: `rescore_existing.py`
refuses to revive a row carrying it (rule 19), because a clean rescore can't
know whether the news gate would still reject it.

Usage:
    python cleanup_news_filters.py               # live
    python cleanup_news_filters.py --dry-run     # print what would go
"""
from __future__ import annotations

import argparse
import time

from news import _word_hit
from src.config import env, load_config
from src.notion_client import NotionClient

# A stored NEWS title from a Google News feed looks like
# "<headline> - <Publisher>". Only strip when the tail is plausibly a
# publisher name and the head is still a real headline — a bare rsplit
# would maul "Punch List: Skanska wins $1.2B contract - Construction Dive"
# no worse, but would also eat the back half of a genuinely hyphenated
# headline. The asymmetric checks above absorb a wrong guess either way.
MAX_PUBLISHER_LEN = 60
MIN_HEADLINE_LEN = 20


def _strip_publisher(title: str) -> str:
    if " - " not in title:
        return title
    head, tail = title.rsplit(" - ", 1)
    if len(tail) <= MAX_PUBLISHER_LEN and len(head) >= MIN_HEADLINE_LEN:
        return head
    return title


def _prop_select(row: dict, name: str) -> str:
    return (((row.get("properties") or {}).get(name) or {}).get("select") or {}).get("name", "")


def _prop_rt(row: dict, name: str) -> str:
    vals = ((row.get("properties") or {}).get(name) or {}).get("rich_text", [])
    return vals[0].get("plain_text", "") if vals else ""


def should_disqualify(row: dict, cfg: dict) -> str | None:
    """Disqualify reason if this NEWS row would fail the current news gate,
    else None. Non-NEWS rows always return None — they belong to
    cleanup_filters.py."""
    if _prop_select(row, "Source") != "NEWS":
        return None
    title = "".join(t.get("plain_text", "") for t in
                    ((row.get("properties") or {}).get("Name") or {}).get("title", []))
    if not title:
        return None

    news = cfg["news"]
    stripped = _strip_publisher(title)

    for kw in news.get("exclude_keywords", []):
        if _word_hit(stripped, [kw]):
            return f"matches exclude keyword '{kw}'"

    if not _word_hit(title, news["gate_keywords"]) and \
            not _word_hit(stripped, news["gate_keywords"]):
        return "no award/start signal in the headline"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    notion = NotionClient(env("NOTION_TOKEN"), cfg)

    rows = notion.query_all_rows({"property": "Status", "select": {"equals": "New"}})
    news_rows = [r for r in rows if _prop_select(r, "Source") == "NEWS"]
    print(f"Scanning {len(news_rows)} 'New' NEWS rows (of {len(rows)} 'New' total)")

    flagged = 0
    for row in news_rows:
        reason = should_disqualify(row, cfg)
        if not reason:
            continue
        title = notion.row_title(row)
        print(f"  {title[:90]} — {reason}")
        flagged += 1
        if not args.dry_run:
            note = f"Auto-disqualified (retroactive news filter cleanup): {reason}"
            existing = _prop_rt(row, "Fit reason")
            combined = f"{existing} · {note}" if existing else note
            notion.update_properties(row["id"], {
                "Status": {"select": {"name": "Disqualified"}},
                "Fit reason": NotionClient._rt(combined),
            })
            time.sleep(0.4)

    verb = "Would disqualify" if args.dry_run else "Disqualified"
    print(f"{verb} {flagged} of {len(news_rows)} NEWS rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
