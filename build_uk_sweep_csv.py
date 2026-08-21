"""Turns the 007-location-sweep JSON (uk-all-regions-records.json, 109 UK
records, Aug 2026) into the master-schema CSV that import_scored_csv.py
reads. Writes the final master-schema CSV: sweep fields + a backfill of the three
inputs score_project() needs (Use case, Concrete opportunity, AE), each
derived from evidence already in the record. Deliberately conservative:
where a record does not establish scope, the field is left blank so the
fit-out rule sends it Low rather than being talked up."""
import csv, json, re, sys, collections
from src.config import load_config
from src.routing import resolve_ae
from src.notion_client import NotionClient as N

PATTERNS = {
    "Slipform core":        [r"slip-?\s?form", r"jump-?\s?form"],
    "Tunnel linings":       [r"tunnel lining", r"segmental lining", r"sprayed concrete lining",
                             r"\bscl\b", r"segment casting", r"tbm segment", r"precast segment"],
    "Mass concrete":        [r"mass concrete", r"mass pour", r"mass-concrete"],
    "Post-tensioned slabs": [r"post-?\s?tension"],
    "Raft / substructure / basement": [r"\braft\b", r"substructure", r"basement",
                                       r"diaphragm wall", r"box structure"],
    "Piling / foundations": [r"\bpiling\b", r"\bpiled?\b", r"bored pile", r"secant pile",
                             r"\bcfa\b", r"foundation"],
    "In-situ frame":        [r"in-?situ frame", r"in situ frame", r"\brc frame\b",
                             r"reinforced concrete frame", r"concrete frame", r"frame subcontract",
                             r"superstructure frame", r"\bframe programme\b"],
    "Precast":              [r"pre-?cast"],
    "DfMA / modular":       [r"\bdfma\b", r"\bmodular\b", r"\bmmc\b", r"volumetric",
                             r"off-?site manufactur"],
    "Marine / water-retaining": [r"\bmarine\b", r"\bquay\b", r"\bjetty\b", r"caisson",
                                 r"water-retaining", r"\breservoir\b", r"sea ?wall",
                                 r"\bharbour\b", r"\block gate\b"],
    "Culverts / ancillary structures": [r"\bculvert\b", r"headwall", r"retaining wall",
                                        r"ancillary structure"],
    "Slabs / pavements":    [r"\bpavement\b", r"slab on grade", r"ground slab", r"\brunway\b",
                             r"hardstanding", r"\bapron\b", r"concrete surfaces?\b"],
}
# mega-scale concrete markers — a pour programme, not a building's worth
MEGA = [r"\btbm\b", r"\btunnel\b", r"nuclear", r"sizewell", r"\bhs2\b", r"\bdam\b",
        r"reservoir", r"viaduct", r"\bcaisson\b", r"mass concrete"]
SMALL = [r"localised", r"fit-?out", r"refurbishment only", r"events pavilion",
         r"\bpublic realm\b"]


# ordered most-specific-first; first hit wins
PTYPE = [
    ("Nuclear",            [r"sizewell", r"hinkley", r"sellafield", r"\bnuclear\b", r"\bsmr\b"]),
    ("Energy from waste",  [r"energy from waste", r"\befw\b", r"incinerat"]),
    ("Precast yard / batching plant", [r"precast yard", r"batching plant"]),
    ("Tunnel",             [r"\btunnel\b", r"\btbm\b"]),
    ("Airport",            [r"\bairport\b", r"\brunway\b", r"\btaxiway\b", r"airfield"]),
    ("Rail / metro",       [r"\bhs2\b", r"network rail", r"\brail\b", r"\bmetro\b",
                            r"\btram\b", r"station upgrade", r"\btru\b", r"\btfl\b"]),
    ("Bridge / viaduct",   [r"\bbridge\b", r"\bviaduct\b"]),
    ("Highway / road",     [r"\bhighways?\b", r"road scheme", r"\broads\b", r"motorway",
                            r"\bjunction\b", r"lower thames crossing"]),
    ("Port / marine",      [r"\bport\b", r"\bquay\b", r"\bharbour\b", r"\bdocks?\b", r"\bjetty\b"]),
    ("Dam / hydro / reservoir", [r"\breservoir\b", r"\bdam\b", r"\bhydro\b"]),
    ("Flood & coastal defence", [r"flood defence", r"coastal defence", r"sea ?wall", r"flood alleviation"]),
    ("Wastewater treatment",[r"wastewater", r"sewage", r"\bwwtw\b"]),
    ("Water treatment",    [r"water treatment", r"\bwtw\b", r"potable"]),
    ("Power & transmission",[r"substation", r"transmission", r"power station", r"\bgrid\b",
                            r"offshore wind", r"\bsolar farm\b"]),
    ("Data centre",        [r"data centre", r"data center"]),
    ("Life sciences / R&D",[r"life science", r"laborator", r"\br&d\b", r"space skills"]),
    ("Student accommodation", [r"\bpbsa\b", r"student accommodation", r"student scheme",
                            r"student tower", r"co-?living", r"student village",
                            r"student halls", r"halls of residence", r"-bed student"]),
    ("Hospital / healthcare", [r"\bhospital\b", r"healthcare", r"health centre", r"\bnhs\b"]),
    ("Prison / justice / defence", [r"\bprison\b", r"\bdefence\b", r"\bbarracks\b",
                            r"\bmod\b", r"\bawe\b"]),
    ("Stadium / arena / convention", [r"\bstadium\b", r"\barena\b", r"convention"]),
    ("Logistics / warehouse", [r"logistics", r"warehouse", r"big box", r"distribution centre"]),
    ("Industrial / manufacturing", [r"manufactur", r"\bfactory\b", r"gigafactory", r"industrial"]),
    ("Education",          [r"\buniversity\b", r"\bschool\b", r"\bcampus\b", r"\bcollege\b"]),
    ("Residential",        [r"build-?to-?rent", r"\bbtr\b", r"residential", r"\bhomes\b",
                            r"apartments", r"housing"]),
    ("Commercial / office",[r"\boffice\b", r"workspace", r"commercial floor"]),
    ("Retail / hotel / leisure", [r"\bhotel\b", r"\bretail\b", r"\bleisure\b",
                            r"shopping centre", r"retail park", r"leisure centre"]),
]


def project_type(txt):
    for name, pats in PTYPE:
        if any(re.search(p, txt) for p in pats):
            return name
    return ''

def storeys(txt):
    return max([int(m) for m in re.findall(r"(\d{1,2})[- ]store(?:y|ys|ies)", txt)] or [0])

cfg = load_config()
data = json.load(open(sys.argv[1]))
recs = data["records"]

READ = ["Name", "General contractor/JV", "JV / parents", "Concrete subcontractor",
        "Client", "Location", "Country", "Region", "Lat", "Lng", "Value",
        "Project type", "Work nature", "Project stage", "Concrete opportunity",
        "Use case", "Competitor present", "Expected concrete start",
        "Expected completion", "Summary", "Source", "Status", "Verified",
        "Notice URL", "AE"]
CARRIED = ["Notes", "Enrich", "Build contacts", "Create deal", "Announced",
           "Tender deadline", "Fit", "Fit profile", "Fit reason",
           "Fit dimensions", "Product fit", "Partner route", "SDR", "Value band"]

stats = collections.Counter()
rows = []
for r in recs:
    p = dict(r["properties"])
    for k in list(p):
        if k.startswith("date:") and k.endswith(":start"):
            p[k.split(":")[1]] = p.pop(k)
        elif k.startswith("date:"):
            p.pop(k)

    txt = " ".join([r.get("body", ""), p.get("Summary", ""), p.get("Fit reason", ""),
                    p.get("Fit dimensions", ""), p.get("Name", ""),
                    p.get("Expected concrete start", "")]).lower()

    # --- Use case
    ucs = [uc for uc, pats in PATTERNS.items() if any(re.search(x, txt) for x in pats)]
    p["Use case"] = ucs
    stats["use case set" if ucs else "use case blank"] += 1

    # --- Project type (sector; second half of _match_profiles' signal)
    if not (p.get('Project type') or '').strip():
        pt = project_type(" ".join([p.get("Name",""), p.get("Summary","")]).lower())
        p['Project type'] = pt
        stats[f"ptype derived: {pt or '(none)'}"] += 1

    # --- Concrete opportunity (only where scope exists to size)
    opp = (p.get("Concrete opportunity") or "").strip()
    if not opp and ucs:
        st = storeys(txt)
        # scale evidence is tested BEFORE the downgrade: a passing mention of
        # "public realm" or a fit-out package must not shrink a 23-storey
        # frame job (CLAUDE.md rule 9 — keywords are never the primary signal)
        scale = (st >= 10 or "repeating floorplate" in txt
                 or {"Raft / substructure / basement", "In-situ frame",
                     "Slipform core"} & set(ucs))
        if any(re.search(x, txt) for x in MEGA) or st >= 30:
            opp = "Large"
        elif scale:
            opp = "Medium"
        elif any(re.search(x, txt) for x in SMALL):
            opp = "Small"
        p["Concrete opportunity"] = opp
        stats[f"opportunity derived: {opp or '(left blank)'}"] += 1
    elif opp:
        stats[f"opportunity explicit: {opp}"] += 1
    else:
        stats["opportunity blank (no scope)"] += 1

    # --- AE: explicit kept, blanks via the repo's own routing rule
    if not (p.get("AE") or "").strip():
        p["AE"] = resolve_ae(p.get("General contractor/JV", ""), "GB", cfg) or "unassigned"
        stats[f"AE derived: {p['AE']}"] += 1
    else:
        stats[f"AE explicit: {p['AE']}"] += 1

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
