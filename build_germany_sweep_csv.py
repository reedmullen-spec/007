"""Turns the 007-location-sweep JSON (germany-all-states-records.json, 93
records across all 16 Bundesländer, Aug 2026) into the master-schema CSV
that import_scored_csv.py reads.

Much thinner than build_uk_sweep_csv.py: the German sweep already carries
Use case, Concrete opportunity and AE on every record, so none of the
UK builder's evidence-derived backfill is needed. What is needed is
vocabulary normalisation — the sweep exporter writes short project-type
labels and one use-case name that are not options in the live schema, and
import_scored_csv.py drops any select value it cannot match exactly
(_select_prop returns None), which would silently lose the field *and*
feed score_project() a project type it has no rule for. Mapped here so
the substitution is on the record rather than buried in a loader:

  - Project type: the sweep's "Bridge"/"Road"/"Rail"/"Port"/"Industrial"
    are shortened forms of real options. "Lock / water infrastructure" is
    not — all five rows are navigation locks (WNA Datteln canal locks,
    Große Seeschleuse Emden, Nord-Ostsee-Kanal), so they map to
    "Port / marine"; the other candidate options ("Dam / hydro /
    reservoir", "Water treatment") describe different structures.
  - Use case: the sweep's "Slab / floorplate" is "Slabs / pavements" in
    the schema. Multi-selects are joined with "," because that is
    import_scored_csv.MULTI_SELECT_SEP — the CSV exported alongside this
    JSON used "; " instead, which the loader would have parsed as one
    unmatched string and dropped for 87 of 93 rows.
  - "date:Expected completion:start" is flattened to the plain
    "Expected completion" column the loader actually reads (the paired
    ":is_datetime" column is dropped), same as the UK builder does.

Values are EUR millions and are passed through as-is: the loader
multiplies by 1e6 and bands the result with no FX conversion, so bands
are EUR-denominated on these rows. 56 of 93 records have no value at all.

Usage:
    python build_germany_sweep_csv.py data/germany-all-states-records.json \
        data/germany_all_states_2026-08-21.csv
"""
import csv, json, sys, collections

sys.path.insert(0, ".")
from src.notion_client import NotionClient as N

PROJECT_TYPE_MAP = {
    "Bridge": "Bridge / viaduct",
    "Road": "Highway / road",
    "Rail": "Rail / metro",
    "Port": "Port / marine",
    "Industrial": "Industrial / manufacturing",
    "Lock / water infrastructure": "Port / marine",
}
USE_CASE_MAP = {"Slab / floorplate": "Slabs / pavements"}

READ = ["Name", "General contractor/JV", "JV / parents", "Concrete subcontractor",
        "Client", "Location", "Country", "Region", "Lat", "Lng", "Value",
        "Project type", "Work nature", "Project stage", "Concrete opportunity",
        "Use case", "Competitor present", "Expected concrete start",
        "Expected completion", "Summary", "Source", "Status", "Verified",
        "Notice URL", "AE"]
CARRIED = ["Notes", "Enrich", "Build contacts", "Create deal", "Fit",
           "Fit profile", "Fit reason", "Fit dimensions", "Product fit",
           "Partner route", "SDR", "Value band", "Notice ID"]

recs = json.load(open(sys.argv[1], encoding="utf-8"))["records"]
stats = collections.Counter()
rows = []

for r in recs:
    p = dict(r["properties"])

    # flatten the exporter's date:<prop>:start columns, drop :is_datetime
    for k in list(p):
        if k.startswith("date:") and k.endswith(":start"):
            p[k.split(":")[1]] = p.pop(k)
        elif k.startswith("date:"):
            p.pop(k)

    pt = (p.get("Project type") or "").strip()
    if pt in PROJECT_TYPE_MAP:
        p["Project type"] = PROJECT_TYPE_MAP[pt]
        stats[f"project type mapped: {pt} -> {p['Project type']}"] += 1
    elif pt and pt not in N.PROJECT_TYPES:
        stats[f"project type UNMAPPED (will be dropped): {pt}"] += 1
    elif pt:
        stats[f"project type already valid: {pt}"] += 1

    uc = p.get("Use case") or []
    uc = [uc] if isinstance(uc, str) else list(uc)
    mapped = []
    for u in uc:
        u = (u or "").strip()
        if not u:
            continue
        if u in USE_CASE_MAP:
            stats[f"use case mapped: {u} -> {USE_CASE_MAP[u]}"] += 1
            u = USE_CASE_MAP[u]
        if u in N.USE_CASES:
            mapped.append(u)
        else:
            stats[f"use case UNMAPPED (will be dropped): {u}"] += 1
    p["Use case"] = mapped
    if not mapped:
        stats["no use case (weakens Fit)"] += 1

    if not (p.get("Value") or "").strip() if isinstance(p.get("Value"), str) \
            else p.get("Value") in (None, ""):
        stats["no value"] += 1

    out = {}
    for col in READ + CARRIED:
        v = p.get(col, "")
        out[col] = ",".join(v) if isinstance(v, list) else ("" if v is None else v)
    rows.append(out)

with open(sys.argv[2], "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=READ + CARRIED)
    w.writeheader(); w.writerows(rows)

for k, v in sorted(stats.items()): print(f"  {v:4d}  {k}")
print(f"\n{len(rows)} rows -> {sys.argv[2]}")
