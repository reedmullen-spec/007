"""Reports which of the 007 private app's scopes are actually usable.

Scopes have twice been the thing quietly breaking a feature — notes.read
missing while `_push_summary_note()` degraded on purpose (PR #14), and a
contacts-schema 403 killing every `Build contacts` run without any message
naming a scope. Neither had a symptom you could look for on purpose. This
gives all of it one answer.

`GET /oauth/v1/access-tokens/<token>` returns 400 for private-app tokens, so
the scope list cannot simply be read back: each capability has to be probed
against the real endpoint. Every probe below is a GET, so this is safe to run
at any time. `HUBSPOT_TOKEN` is an Actions secret and never present locally,
which is why this is a workflow rather than a one-liner.

Each probe reports the endpoint the *product code* calls, not a synthetic
equivalent, so a probe passing means that code path works:

    notes read      deal_note_bodies()       -> the summary-note marker dedup
    contacts schema ensure_linkedin_property() -> Build contacts, every run
    deals schema    ensure_notice_property() / ensure_summary_property()
                                             -> every actions.py run's preamble
    leads objects   find_lead_by_notice_id() -> Create lead
    leads schema    ensure_lead_notice_property() -> Create lead

Two things the leads probes cannot tell you, both write-side and therefore
deliberately untested here: whether crm.objects.leads.write and
crm.objects.companies.write are granted (find_or_create_company creates the
company the lead hangs off), and whether `lead_property_group` in
config.yaml names a real property group on the leads object. A read probe
passing means Create lead gets as far as its first write.

    python check_hubspot_scopes.py
"""
from __future__ import annotations

import sys

import requests

from src.config import env, load_config
from src.hubspot_client import BASE, HubSpotClient

# capability -> (scope that grants it, what breaks without it)
BLAST_RADIUS = {
    "notes read": (
        "crm.objects.notes.read",
        "_push_summary_note() 403s on its marker read and skips the note on "
        "any deal that already exists; a note lost mid-action cannot be "
        "recovered by re-ticking Create deal"),
    "contacts schema read": (
        "crm.schemas.contacts.read",
        "ensure_linkedin_property() raises before it can create anything, so "
        "every Build contacts action fails — and because it dies on the read, "
        "its own 'add the write scope or create the property by hand' message "
        "never prints"),
    "deals schema read": (
        "crm.schemas.deals.read",
        "actions.py cannot get past ensure_notice_property() in its preamble, "
        "so no action of any kind runs"),
    "leads objects read": (
        "crm.objects.leads.read",
        "Create lead cannot dedup, so it either 403s outright or (worse, if "
        "only the search were blocked) would make a second lead on a "
        "re-ticked row — the other three actions are unaffected, which is "
        "why the lead property bootstrap is not in actions.py's preamble"),
    "leads schema read": (
        "crm.schemas.leads.read",
        "ensure_lead_notice_property() raises before it can create the "
        "notice-id property, so Create lead fails on every row — and since "
        "find_lead_by_notice_id() filters on that property, it would 400 "
        "even if the objects scope were present"),
}


def _verdict(name: str, ok: bool | None, detail: str) -> None:
    mark = {True: "OK  ", False: "DENIED", None: "?   "}[ok]
    print(f"  [{mark}] {name}: {detail}")


def probe_notes(hs: HubSpotClient) -> bool | None:
    """The two calls deal_note_bodies() makes, reported separately: a partial
    grant returns 200 on the association and 403 on the note bodies."""
    r = hs.session.get(f"{BASE}/crm/v3/objects/deals", params={"limit": 1}, timeout=30)
    if r.status_code != 200:
        _verdict("notes read", None, f"cannot list deals ({r.status_code}) — not probed")
        return None
    results = r.json().get("results") or []
    if not results:
        _verdict("notes read", None, "no deals in the portal — nothing to probe")
        return None
    deal_id = results[0]["id"]

    r = hs.session.get(f"{BASE}/crm/v4/objects/deals/{deal_id}/associations/notes",
                       params={"limit": 100}, timeout=30)
    if r.status_code != 200:
        _verdict("notes read", False,
                 f"associations/notes -> {r.status_code} on deal {deal_id}")
        return False
    ids = [a.get("toObjectId") for a in r.json().get("results", []) if a.get("toObjectId")]
    if not ids:
        _verdict("notes read", None,
                 f"associations/notes -> 200, but deal {deal_id} has no notes, "
                 "so notes/batch/read is untested")
        return None

    r = hs.session.post(f"{BASE}/crm/v3/objects/notes/batch/read",
                        json={"properties": ["hs_note_body"],
                              "inputs": [{"id": str(i)} for i in ids]},
                        timeout=30)
    ok = r.status_code in (200, 207)
    _verdict("notes read", ok,
             f"associations/notes -> 200, notes/batch/read -> {r.status_code} "
             f"({len(ids)} note(s) on deal {deal_id})")
    return ok


def probe_leads(hs: HubSpotClient) -> bool | None:
    """Whether the leads object is readable at all.

    Distinguishes the two reasons Create lead can be dead on arrival: a 403
    is the missing scope, but leads is a Sales Hub Professional+ feature, so
    a portal without it answers differently (404/400) and no amount of scope
    granting will help. Worth separating — one is an admin ticket, the other
    is "this action can never work here".
    """
    r = hs.session.get(f"{BASE}/crm/v3/objects/leads", params={"limit": 1}, timeout=30)
    if r.status_code == 200:
        n = len(r.json().get("results") or [])
        _verdict("leads objects read", True,
                 f"GET objects/leads -> 200 ({n} lead(s) visible)")
        return True
    if r.status_code == 403:
        _verdict("leads objects read", False,
                 f"GET objects/leads -> 403: {r.text[:120]}")
        return False
    _verdict("leads objects read", False,
             f"GET objects/leads -> {r.status_code}: {r.text[:120]} — a non-403 "
             "here can mean the leads object isn't enabled on this portal "
             "(Sales Hub Professional+), which no scope change fixes")
    return False


def probe_property(hs: HubSpotClient, label: str, object_type: str,
                   name: str) -> bool | None:
    """The exact GET _ensure_property() opens with. 404 means the schema is
    readable and the property simply isn't there yet — which is a pass for
    read, but means the bootstrap POST still has to succeed on first use."""
    r = hs.session.get(f"{BASE}/crm/v3/properties/{object_type}/{name}", timeout=30)
    if r.status_code == 200:
        _verdict(label, True, f"{object_type}/{name} -> 200 (property exists)")
        return True
    if r.status_code == 404:
        _verdict(label, True,
                 f"{object_type}/{name} -> 404 — schema readable, property "
                 f"absent, so first use needs crm.schemas.{object_type}.write "
                 "(not probed: creating it would be a write)")
        return True
    _verdict(label, False, f"{object_type}/{name} -> {r.status_code}: {r.text[:120]}")
    return False


def main() -> int:
    token = env("HUBSPOT_TOKEN")
    hs = HubSpotClient(token, load_config())
    prop = hs.cfg

    r = requests.get(f"{BASE}/oauth/v1/access-tokens/{token}", timeout=30)
    if r.status_code == 200:
        granted = sorted(r.json().get("scopes") or [])
        print(f"== token introspection: {len(granted)} scopes ==")
        for s in granted:
            print(f"  {s}")
    else:
        print(f"== token introspection unavailable ({r.status_code}) — "
              "probing endpoints instead ==")

    print("\n== probes (all read-only) ==")
    results = {
        "notes read": probe_notes(hs),
        "contacts schema read": probe_property(
            hs, "contacts schema read", "contacts", "linkedin_url"),
        "deals schema read": probe_property(
            hs, "deals schema read", "deals", prop["notice_id_property"]),
        "leads objects read": probe_leads(hs),
        "leads schema read": probe_property(
            hs, "leads schema read", "leads", prop["notice_id_property"]),
    }
    # same scope as the notice-id property, so not a separate verdict — but a
    # missing summary property is worth seeing, since both bootstrap together.
    probe_property(hs, "  (deal summary property)", "deals", prop["summary_property"])

    denied = [k for k, v in results.items() if v is False]
    unknown = [k for k, v in results.items() if v is None]

    print("\n== result ==")
    if denied:
        for k in denied:
            scope, impact = BLAST_RADIUS[k]
            print(f"  DENIED {k} — missing {scope}")
            print(f"         {impact}")
    if unknown:
        print(f"  INCONCLUSIVE: {', '.join(unknown)} — see the probe lines above")
    if not denied and not unknown:
        print("  every probed capability is available")
    return 1 if denied else 0


if __name__ == "__main__":
    raise SystemExit(main())
