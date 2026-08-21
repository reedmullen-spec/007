"""HubSpot note bodies for the project summary both deal paths now carry.

`tender_summary` is a deal property, and a property is invisible in the
timeline — which is where an AE actually reads. So the summary goes on as a
note too, from both surfaces that put a deal in HubSpot:

  * `Enrich` (enrich.py) appends the pack's TL;DR under its pack link
  * `Create deal` (actions.py) posts the Notion row's summary

Shared so those two cannot drift apart on SUMMARY_NOTE_MARKER, which is the
only thing stopping a row that gets both from carrying two copies of the same
summary: enrich.py stamps the marker, actions.py checks for it and no-ops.

Rendering mirrors the Notion pack renderer rather than reimplementing it —
the TL;DR is 5 markdown bullets with inline marks (skills/*/SKILL.md), and
escaped-to-plain-text they reach HubSpot showing literal `**` and
`[label](url)`.
"""
from __future__ import annotations

from html import escape

# The pack renderer's inline grammar, reused so a TL;DR means the same thing
# in a HubSpot note as on the Notion page — including the _ESCAPED_STAR park
# for the "5-5.5\*" NABERS ratings the research skills emit, which would
# otherwise eat a bold span here exactly as it used to there.
from src.notion_client import _ESCAPED_STAR, _INLINE

SUMMARY_NOTE_MARKER = "007 project summary"

_BULLET_PREFIXES = ("- ", "* ", "• ")


def _text(raw: str, quote: bool = False) -> str:
    return escape(raw.replace(_ESCAPED_STAR, "*"), quote=quote)


def md_inline_to_html(text: str) -> str:
    """Inline markdown -> the HTML subset HubSpot notes render.

    Same three marks the packs actually contain (**bold**, `code`,
    [label](url)); everything else is escaped text.
    """
    out: list[str] = []
    for part in _INLINE.split(text.replace("\\*", _ESCAPED_STAR)):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append(f"<strong>{_text(part[2:-2])}</strong>")
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(f"<code>{_text(part[1:-1])}</code>")
        elif part.startswith("[") and part.endswith(")") and "](" in part:
            label, _, href = part[1:-1].partition("](")
            out.append(f'<a href="{_text(href, quote=True)}">{_text(label)}</a>')
        else:
            out.append(_text(part))
    return "".join(out)


def render_summary_body(summary: str, source: str) -> str:
    """Summary text -> marker header + HTML body.

    Bullet runs become a real <ul> (the TL;DR is always 5 bullets), blank
    lines separate paragraphs, and single newlines inside a paragraph become
    <br /> — a wall of text is the thing this note exists to avoid.
    """
    html = [f"<p><strong>{SUMMARY_NOTE_MARKER}</strong> — {escape(source)}</p>"]
    bullets: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if bullets:
            html.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()
        if para:
            html.append(f"<p>{'<br />'.join(para)}</p>")
            para.clear()

    for line in (raw.strip() for raw in summary.strip().splitlines()):
        if not line:
            flush()
        elif line[:2] in _BULLET_PREFIXES:
            if para:
                flush()
            bullets.append(md_inline_to_html(line[2:].strip()))
        else:
            if bullets:
                flush()
            para.append(md_inline_to_html(line))
    flush()
    return "".join(html)
