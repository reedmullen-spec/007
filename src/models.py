"""Data models shared across the radar."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


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
        digest = hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:16]
        return f"NEWS:{digest}"
