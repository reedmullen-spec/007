"""Project fit scoring — modelled on the company ICP scorer.

Why it's built this way (lessons from the company scoring set, n=123):

1. A score is always RELATIVE TO A NAMED PROFILE. Every company explanation
   says which ICP it matched. "High" alone is unactionable; "High for
   Mass-concrete civils" tells an AE what to pitch.
2. FOUR bands, not three. Low ("adjacent, maybe later" — fit-out, rentals)
   is materially different from Disqualified ("never" — membership bodies,
   media). Their set: 45 High, 55 Medium, 8 Low, 15 Disqualified.
3. ACTIVITY beats size. A 95k-employee firm scores Low if it doesn't execute
   structural work; a 1.1k contractor scores High. Median employees by band
   runs *backwards* (High 1,099 < Medium 1,813 < Disqualified 3,635) — proof
   the model isn't just rewarding scale.
4. SCALE IS A MODIFIER, NOT A GATE. 13 of 55 Mediums are "right industry,
   size exceeds the ICP range" — still worth working, just not perfect.
5. The reason NAMES THE DECIDING DIMENSION in one sentence.

Improvement over theirs: their inputs (industry, employee count) are facts,
so their scores are reproducible. Ours must be too — so every dimension here
is computed deterministically from the qualify output. The model supplies
observations (use case, work nature, timing); the RULES decide the band. Same
project scored twice always lands in the same band, and the rubric is tunable
in one place instead of buried in a prompt.

Aug 2026 revision (Issam's biggest accuracy ask): a past concrete start is
NOT disqualifying on its own — phased pours mean foundations now, a pause,
then structure later, so a start up to 12 months ago still reads as a live
site. The ONLY automatic disqualification left is a project that's already
Complete, or one whose expected completion is under 6 months away (too late
to spec anything in). Project stage is now its own scoring dimension —
on-site/PCSA/awarded work can reach High; still-at-tender work is real
pipeline but caps at Medium (Q1+ booking, not this quarter).
"""
from __future__ import annotations

import datetime as dt
import re

# --------------------------------------------------------------------------
# Project profiles — the analogue of their ICPs. Each carries its own ideal
# timing window and minimum opportunity, exactly as their ICPs carry their
# own employee ranges.

OPPORTUNITY_RANK = {"Small": 1, "Medium": 2, "Large": 3, "Unknown": 0}

PROFILES = [
    {
        "name": "Mass-concrete civils",
        "products": ["Cure / Signal", "Data Hub"],
        "use_cases": {"Mass concrete", "Tunnel linings", "Marine / water-retaining",
                      "Piling / foundations", "Culverts / ancillary structures"},
        "types": {"Bridge / viaduct", "Tunnel", "Dam / hydro / reservoir",
                  "Port / marine", "Flood & coastal defence", "Water treatment",
                  "Wastewater treatment", "Nuclear", "Power & transmission",
                  "Rail / metro", "Highway / road"},
        # Thermal control is effectively mandatory, so engage close to the pour.
        "timing_months": (0, 24),
        "min_opportunity": "Medium",
    },
    {
        "name": "Schedule-critical vertical build",
        "products": ["Cure / Signal", "Data Hub"],
        "use_cases": {"In-situ frame", "Slipform core", "Post-tensioned slabs",
                      "Raft / substructure / basement"},
        "types": {"Commercial / office", "Residential", "Data centre",
                  "Student accommodation", "Hospital / healthcare",
                  "Retail / hotel / leisure", "Stadium / arena / convention",
                  "Logistics / warehouse", "Life sciences / R&D"},
        # Cycle-time value lands when the frame is imminent.
        "timing_months": (0, 18),
        "min_opportunity": "Small",
    },
    {
        "name": "Industrialised construction / DfMA",
        "products": ["FieldAtlas"],
        "use_cases": {"DfMA / modular", "Precast"},
        "types": {"Prison / justice / defence", "Hospital / healthcare",
                  "Education", "Residential", "Data centre",
                  "Industrial / manufacturing", "Nuclear"},
        # Specification decisions are made at PCSA — engage EARLY, so a long
        # window is a feature here, unlike the civils profile.
        "timing_months": (6, 42),
        "min_opportunity": "Medium",
    },
    {
        "name": "Low-carbon spec-in",
        "products": ["MixAI", "Data Hub"],
        "use_cases": set(),          # triggered by carbon signals, not scope
        "types": set(),
        "timing_months": (0, 36),
        "min_opportunity": "Small",
        "requires_carbon_signal": True,
    },
    {
        "name": "Precast & batching supply",
        "products": ["Data Hub", "MixAI"],
        "use_cases": {"Precast"},
        "types": {"Precast yard / batching plant"},
        "timing_months": (0, 60),
        "min_opportunity": "Small",
    },
]

CARBON_SIGNALS = ("net zero", "net-zero", "low carbon", "low-carbon", "carbon negative",
                  "carbon-negative", "embodied carbon", "co2", "co₂", "epd",
                  "prestatieladder", "ghg", "decarbon", "sustainab")

# Project stages where the concrete is already gone. "Finishing" is
# deliberately NOT here — punch-list/finishing trades can still involve
# concrete (screeds, repairs); only a fully Complete project is spent.
SPENT_STAGES = {"Complete"}

# Active-pipeline stages for the "stage" dimension (Issam's biggest
# accuracy ask) — on site, in preconstruction, or already awarded.
HIGH_STAGES = {"Main works / on site", "Groundbreaking / enabling works",
               "PCSA / preconstruction", "Awarded - pre-start"}

# How far into the past a concrete start can be and still read as a live
# site rather than a stale notice — phased pours (foundations now, a
# pause, then structure later) are normal on any real job of size.
PAST_START_GRACE_MONTHS = 12

# A project completing sooner than this has no runway left to sell into.
MIN_MONTHS_TO_COMPLETION = 6


# --------------------------------------------------------------------------

def resolve_date(text: str) -> dt.date | None:
    """Parse an exact ISO date (round-tripping a stored Notion date property)
    or a fuzzy estimate like 'Q3 2027' / '2027' / 'Live' / 'Unknown' into a
    representative calendar date (mid-quarter / mid-year). None when
    unparseable or unknown."""
    if not text:
        return None
    s = text.strip()
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        pass

    s = s.lower()
    if s in ("unknown", "tbc", "n/a", ""):
        return None
    if s in ("live", "now", "on site", "underway", "started"):
        return dt.date.today()

    q = re.search(r"q([1-4])\s*[/ -]?\s*(20\d{2})", s)
    if q:
        quarter, year = int(q.group(1)), int(q.group(2))
        month = (quarter - 1) * 3 + 2          # mid-quarter
    else:
        y = re.search(r"(20\d{2})", s)
        if not y:
            return None
        year, month = int(y.group(1)), 6       # mid-year
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", s)
        if m:
            month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                     "sep", "oct", "nov", "dec"].index(m.group(1)) + 1
    return dt.date(year, month, 15)


def _months_from(target: dt.date | None) -> float | None:
    if target is None:
        return None
    today = dt.date.today()
    return (target.year - today.year) * 12 + (target.month - today.month)


def _months_until(concrete_start: str) -> float | None:
    """Months from today until `concrete_start`. None when unknown, 0 for
    live/imminent, negative for past."""
    return _months_from(resolve_date(concrete_start))


def _has_carbon_signal(text: str) -> bool:
    low = (text or "").lower()
    return any(sig in low for sig in CARBON_SIGNALS)


def has_structural_scope(fields: dict) -> bool:
    """The activity test. Their scorer's decisive rule: a firm in the right
    sector still scores Low if it doesn't execute structural work (fit-out,
    rental, property services). Same here — an office FIT-OUT is the right
    project type with the wrong activity, and must never score High."""
    return bool(set(fields.get("use_case") or []) - {"Unknown", ""})


def _match_profiles(fields: dict) -> list[dict]:
    """Every profile this project plausibly belongs to.

    Sector alone is never enough: a project type may only pull in a profile
    when a structural use case is also present."""
    use_cases = set(fields.get("use_case") or [])
    structural = has_structural_scope(fields)
    ptype = fields.get("project_type", "")
    carbon = _has_carbon_signal(
        " ".join(str(fields.get(k, "")) for k in ("summary", "fit_reason", "title")))

    matched = []
    for p in PROFILES:
        if p.get("requires_carbon_signal"):
            if carbon:
                matched.append(p)
            continue
        if use_cases & p["use_cases"]:
            matched.append(p)
        elif ptype in p["types"] and structural:
            matched.append(p)
    return matched


def _disqualify(fields: dict, cfg: dict) -> str | None:
    """Hard exclusions. Deliberately narrow — everything else (unclear
    scope, stale-looking timing, wrong activity) is a soft dimension that
    caps the band instead of killing the row outright; see has_
    structural_scope()'s activity gate and the "stage"/"timing" dimensions
    below. Returns the reason, or None to continue scoring."""
    stage = fields.get("project_stage", "")
    if stage in SPENT_STAGES:
        return "project is complete — no concrete opportunity remains"

    completion = resolve_date(fields.get("expected_completion", ""))
    months_to_completion = _months_from(completion)
    if months_to_completion is not None and months_to_completion < MIN_MONTHS_TO_COMPLETION:
        return (f"expected completion is {int(months_to_completion)} months away — "
                f"too late to sell into")

    floor = (cfg.get("scoring", {}) or {}).get("disqualify_below_value", 0)
    value = fields.get("value")
    if floor and value is not None and value < floor:
        return f"value below the {floor:,.0f} floor"
    return None


def _dimensions(fields: dict, profile: dict, cfg: dict) -> dict[str, tuple[bool, str]]:
    """Per-dimension pass/fail with a short note, mirroring their
    'size within/exceeds the target range' reasoning."""
    dims: dict[str, tuple[bool, str]] = {}

    # Timing — profile-specific window (the analogue of their employee range),
    # extended into the past by PAST_START_GRACE_MONTHS: a start already
    # underway still reads as a live site (phased pours), not a stale notice.
    months = _months_until(fields.get("expected_concrete_start", ""))
    lo, hi = profile["timing_months"]
    lookback = lo - PAST_START_GRACE_MONTHS
    if months is None:
        dims["timing"] = (False, "concrete start date unknown")
    elif months < lookback:
        dims["timing"] = (False, f"concrete start was {abs(int(months))} months ago, "
                                 f"beyond the {PAST_START_GRACE_MONTHS}-month phased-pour "
                                 f"look-back")
    elif months > hi:
        dims["timing"] = (False, f"concrete start is {int(months)} months out, beyond "
                                 f"the {hi}-month window for this profile")
    elif months < lo:
        dims["timing"] = (True, f"concrete start was {abs(int(months))} months ago — "
                                f"within the {PAST_START_GRACE_MONTHS}-month phased-pour "
                                f"look-back")
    else:
        dims["timing"] = (True, f"concrete start sits inside the {lo}-{hi} month window")

    # Opportunity — our prize, not the headline value.
    opp = fields.get("concrete_opportunity", "Unknown")
    need = profile["min_opportunity"]
    if OPPORTUNITY_RANK.get(opp, 0) == 0:
        dims["opportunity"] = (False, "concrete opportunity not yet sized")
    elif OPPORTUNITY_RANK[opp] < OPPORTUNITY_RANK[need]:
        dims["opportunity"] = (False, f"{opp.lower()} concrete opportunity is below the "
                                      f"{need.lower()} threshold for this profile")
    else:
        dims["opportunity"] = (True, f"{opp.lower()} concrete opportunity")

    # Access — is there a route in?
    gc = (fields.get("gc") or "").strip()
    route = fields.get("partner_route", "TBD")
    if gc:
        dims["access"] = (True, f"delivery entity identified ({gc})")
    elif route in ("White Cap", "Hakron", "Agency"):
        dims["access"] = (True, f"partner route available ({route})")
    else:
        dims["access"] = (False, "no contractor resolved and no partner route")

    # Coverage — can we actually serve it?
    ae = fields.get("ae", "unassigned")
    dims["coverage"] = ((ae != "unassigned"),
                        f"owned by {ae}" if ae != "unassigned" else "no AE assigned")

    # Stage — where the project actually is in its lifecycle. On site,
    # in preconstruction, or already awarded is active pipeline; still at
    # tender is real but not a this-quarter booking.
    stage = fields.get("project_stage", "")
    if stage in HIGH_STAGES:
        dims["stage"] = (True, f"{stage.lower()} — active pipeline")
    elif stage == "Tender":
        dims["stage"] = (False, "still at tender — real pipeline, but a Q1+ "
                                "booking, not this quarter")
    else:
        dims["stage"] = (False, f"stage is {stage.lower() or 'unknown'}, not yet "
                                f"active pipeline")
    return dims


def score_project(fields: dict, cfg: dict | None = None) -> dict:
    """Score one project. Returns band, profile, reason, products, dimensions.

    Bands mirror the company scorer:
      Disqualified — structurally impossible: project is Complete, or
                     completion is under MIN_MONTHS_TO_COMPLETION away
                     (deliberately narrow — everything else is a soft
                     dimension below, not a hard kill)
      Low          — real concrete but wrong shape for every profile, or 2+
                     dimensions off (their 'fit-out / rental' equivalent)
      Medium       — matches a profile, exactly one dimension off (their
                     'right industry, size exceeds range' equivalent) — e.g.
                     still at tender: real pipeline, not this quarter
      High         — matches a profile on every dimension, including stage
                     (on site / PCSA / awarded)
    """
    cfg = cfg or {}

    dq = _disqualify(fields, cfg)
    if dq:
        return {"fit": "Disqualified", "profile": "", "products": [],
                "reason": f"Disqualified: {dq}.", "dimensions": {}}

    # Activity gate, before profile matching: no structural scope, no High.
    if not has_structural_scope(fields):
        return {"fit": "Low", "profile": "", "products": [],
                "reason": ("Low: no structural concrete scope established — "
                           "right sector, wrong activity (the fit-out rule)."),
                "dimensions": {}}

    matched = _match_profiles(fields)
    if not matched:
        return {"fit": "Low", "profile": "", "products": [],
                "reason": ("Low: no Converge profile matches this scope — concrete "
                           "work is incidental rather than structural."),
                "dimensions": {}}

    # Score every matched profile, keep the best (their model picks the
    # best-fitting ICP and names it).
    best = None
    for profile in matched:
        dims = _dimensions(fields, profile, cfg)
        off = [name for name, (ok, _) in dims.items() if not ok]
        band = "High" if not off else "Medium" if len(off) == 1 else "Low"
        rank = {"High": 0, "Medium": 1, "Low": 2}[band]
        if best is None or rank < best[0]:
            best = (rank, band, profile, dims, off)

    _, band, profile, dims, off = best

    if band == "High":
        detail = ", ".join(dims[k][1] for k in ("stage", "timing", "opportunity", "access"))
        reason = f"High for {profile['name']}: {detail}."
    elif band == "Medium":
        reason = (f"Medium for {profile['name']}: fits the profile but "
                  f"{dims[off[0]][1]}.")
    else:
        reasons = "; ".join(dims[o][1] for o in off[:2])
        reason = f"Low for {profile['name']}: {reasons}."

    return {"fit": band, "profile": profile["name"],
            "products": profile["products"], "reason": reason,
            "dimensions": {k: {"pass": v[0], "note": v[1]} for k, v in dims.items()}}
