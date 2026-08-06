"""Fetches a URL and extracts readable article text — shared by the
URL-to-project manual-entry path (ingest.py's sweep_manual_rows) and
bulk_import.py, so qualify() works from real article content instead of
a bare title whenever a human pastes a source URL.
"""
from __future__ import annotations

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

USER_AGENT = "Mozilla/5.0 (compatible; 007RadarBot/1.0)"
DROP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form"]


def fetch_page(url: str, max_chars: int = 6000, timeout: int = 20) -> dict:
    """Best-effort readable text + page title. Returns {"title": "",
    "text": ""} on any failure — callers should fall back to whatever
    title they already have rather than block on a flaky fetch."""
    if not url:
        return {"title": "", "text": ""}
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return {"title": "", "text": ""}

    for tag in soup(DROP_TAGS):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())[:max_chars]
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    return {"title": title, "text": text}
