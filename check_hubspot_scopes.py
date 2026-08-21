"""Reports whether the 007 private app's token can read notes yet.

`crm.objects.notes.read` was missing when the summary-note work shipped
(PR #14), and the code degrades on purpose rather than erroring — so there
is no symptom to look for beyond a WARNING line, and nothing that says when
the scope arrives. `HUBSPOT_TOKEN` is an Actions secret and never present
locally, so the check has to run in a workflow.

Read-only: introspects the token, then exercises the exact two calls
`deal_note_bodies()` makes, in order, so a partial grant (association read
allowed, note read denied) is distinguishable from a total one.

    python check_hubspot_scopes.py
"""
from __future__ import annotations

import sys

import requests

from src.config import env, load_config
from src.hubspot_client import BASE, HubSpotClient

NEEDED = "crm.objects.notes.read"


def main() -> int:
    token = env("HUBSPOT_TOKEN")
    cfg = load_config()
    hs = HubSpotClient(token, cfg)

    # ---------------------------------------------------------- scopes
    print("== token introspection ==")
    granted: list[str] = []
    r = requests.get(f"{BASE}/oauth/v1/access-tokens/{token}", timeout=30)
    if r.status_code != 200:
        print(f"  introspection unavailable ({r.status_code}) — "
              "falling back to the live probe only")
    else:
        info = r.json()
        granted = sorted(info.get("scopes") or [])
        print(f"  hub {info.get('hub_id')}  app {info.get('app_id')}")
        print(f"  {len(granted)} scopes granted")
        for s in granted:
            if "notes" in s or "objects.deals" in s:
                print(f"    {s}")
        print(f"  {NEEDED}: {'PRESENT' if NEEDED in granted else 'MISSING'}")

    # ----------------------------------------------------- live probe
    print("\n== live probe (the two calls deal_note_bodies makes) ==")
    r = hs.session.get(f"{BASE}/crm/v3/objects/deals",
                       params={"limit": 1}, timeout=30)
    if r.status_code != 200:
        print(f"  cannot list deals ({r.status_code}): {r.text[:200]}",
              file=sys.stderr)
        return 1
    results = r.json().get("results") or []
    if not results:
        print("  no deals in the portal — nothing to probe against")
        return 0
    deal_id = results[0]["id"]
    print(f"  probing deal {deal_id}")

    r = hs.session.get(f"{BASE}/crm/v4/objects/deals/{deal_id}/associations/notes",
                       params={"limit": 100}, timeout=30)
    print(f"  1. associations/notes    -> {r.status_code}")
    if r.status_code != 200:
        print(f"     {r.text[:200]}")
        print(f"\nRESULT: notes are NOT readable ({NEEDED} still missing)")
        return 1
    ids = [a.get("toObjectId") for a in r.json().get("results", []) if a.get("toObjectId")]
    if not ids:
        print("  2. notes/batch/read      -> skipped (deal has no notes)")
        print("\nRESULT: association read works; batch read untested on this "
              "deal. Scope verdict above is from introspection.")
        return 0

    r = hs.session.post(f"{BASE}/crm/v3/objects/notes/batch/read",
                        json={"properties": ["hs_note_body"],
                              "inputs": [{"id": str(i)} for i in ids]},
                        timeout=30)
    print(f"  2. notes/batch/read      -> {r.status_code} ({len(ids)} note(s))")
    if r.status_code not in (200, 207):
        print(f"     {r.text[:200]}")
        print(f"\nRESULT: notes are NOT readable ({NEEDED} still missing) — "
              "_push_summary_note() will keep skipping the note on any deal "
              "that already exists")
        return 1

    print(f"\nRESULT: notes ARE readable — {NEEDED} is in place, "
          "_push_summary_note()'s marker dedup is live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
