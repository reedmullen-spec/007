"""Data models shared across the radar."""
from __future__ import annotations
 
import hashlib
import re
from dataclasses import dataclass, field
 
 
def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()
 
 
@dataclass
class Project:
    """A tender notice from TED or Find a Tender."""
    source: str                       # "TED" | "FTS"
    notice_id: str                    # stable id -> HubSpot tender_notice_id
    title: str
    url: str = ""
    buyer: str = ""
    country: str = ""
    cpv_codes: list[str] = field(default_factory=list)
    value: float | None = None
    currency: str = "EUR"
    deadline: str = ""
 
    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.notice_id}"
 
 
@dataclass
class NewsItem:
    """A news headline from an RSS feed."""
    region: str                       # uk | eu | us
    entity: str                       # feed label (company/project)
    title: str
    url: str
    published: str = ""
 
    @property
    def dedup_key(self) -> str:
        # Keyed on the normalized HEADLINE, not the URL: the same story often
        # arrives via multiple feeds with different (redirect) URLs.
        digest = hashlib.sha1(_norm_title(self.title).encode("utf-8")).hexdigest()[:16]
        return f"NEWS:{digest}"
 
    @property
    def title_tokens(self) -> set[str]:
        stop = {"the","a","an","to","of","in","on","for","and","with","at","its","as"}
        return {t for t in _norm_title(self.title).split() if t not in stop and len(t) > 2}
