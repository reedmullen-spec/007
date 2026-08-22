"""007 deal-naming convention: '[Contractor] — [Project] — [Location]'.

Contractor and Location are appended only when known — a brand-new deal
often has neither yet (pre-award tender, contractor not yet resolved), in
which case the name is just the project title, same as before this
convention existed. Centralised here so every builder (actions.py,
approvals.py) and every parser (contacts.py, approvals.py) agree on the
exact separator and segment order — getting that out of sync is exactly
what silently corrupted the old 2-part '[Contractor] — [Project]' split
once a third segment showed up in the name.
"""
from __future__ import annotations


def build_deal_name(project: str, contractor: str = "", location: str = "") -> str:
    parts = []
    if contractor:
        parts.append(contractor)
    parts.append(project)
    if location:
        parts.append(location)
    return " — ".join(parts)


def split_deal_name(name: str) -> tuple[str, str]:
    """Recover (contractor, project) from a deal name, ignoring any
    trailing Location segment. Falls back to (name, name) for a deal with
    no '—' at all — a bare title, or a deal predating this convention."""
    parts = [p.strip() for p in name.split("—")]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return name, name


def primary_contractor(gc_jv: str) -> str:
    """The lead contractor out of a stored 'General contractor/JV' value.

    Inverse of NotionClient._gc_jv_text: that helper stores a JV as
    '[Entity] (JV: [Parent], [Parent])', so the raw field is the right
    thing to hand Amplemarket (contacts.py wants every parent name in the
    search) but the wrong thing to name a HubSpot company record. Anything
    needing ONE company — the Create lead action's find_or_create_company —
    takes the entity before the parenthetical.
    """
    return (gc_jv or "").split("(JV:")[0].strip().rstrip(",").strip()
