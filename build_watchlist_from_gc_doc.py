"""Merge the Tier 1/Tier 2 GC list (data/gc_tiers_uk_eu_us_*.csv) into
watchlist.yaml, additively.

Why additive-only: several existing watchlist names are hand-tuned Google
News queries, not legal company names — "Multiplex construction",
"Careys construction", "RG Group construction", "NCC construction" all carry
a disambiguating suffix so the query doesn't drown in unrelated hits. The
document's legal names ("Multiplex Construction Europe", "Careys / Carey
Group") would undo that tuning, and the name is also stored as
NewsItem.entity and seeds the '[Contractor] — ...' HubSpot deal name. So
this script NEVER rewrites or removes an existing entry: it only reports
which document rows are already covered and emits the ones that are not.

Inclusion rule — Tier 1, plus Tier 2 where the document scores concrete fit
as High, minus anything scored Low regardless of tier. Tier 2 High is where
the self-perform frame and groundworks contractors sit (Careys, JRL/Midgard,
Byrne, J Coffey, O'Keefe, Garney's water work), which are the highest
concrete-intensity buyers in the list. Low fit is steel-led, interiors, or
utility/telecom work (Cimolai, William Hare, JRM, MasTec, SOLV Energy) —
a feed for those spends a request to surface work with no concrete in it.

Locales are English everywhere, deliberately. Measured 2026-08-22 across six
major EU contractors: the local-language feeds returned 27 raw entries in
total against 242 for the identical English-locale query, and only the
English ones produced a gate pass. Since every feed is now keyword-gated on
English terms (rule 21), a local-language headline cannot pass the gate —
"VINCI remporte un contrat" carries no gate keyword.

Usage:
    python build_watchlist_from_gc_doc.py            # report + print YAML
    python build_watchlist_from_gc_doc.py --write    # splice into watchlist.yaml
"""
from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "gc_tiers_uk_eu_us_2026-08-22.csv"

# Tokens that carry no identity — legal forms, holding-company words, and the
# generic trade words existing entries use as query disambiguators.
NOISE = {
    "plc", "ltd", "limited", "inc", "corp", "corporation", "co", "cos",
    "company", "companies", "sa", "se", "ag", "nv", "bv", "sgps", "spa",
    "srl", "as", "ab", "asa", "oyj", "zrt", "gmbh", "sas", "a", "the",
    "and", "of", "holding", "holdings", "group", "groep", "gruppe", "grupo",
    "construction", "constructions", "constructors", "contracting",
    "contractors", "building", "builders", "buildings", "enterprises",
}

# Document name -> existing watchlist name, where token normalisation can't
# bridge the gap on its own. Keyed on the folded document name. Each of these
# was surfaced by the near-duplicate report below and confirmed by hand to be
# the same feed as an entry already in the watchlist.
ALIASES = {
    "flatirondragados": "flatiron dragados",
    "bam uk ireland": "BAM UK",
    "multiplex construction europe": "Multiplex construction",
    "graham": "Graham Construction UK",
    "careys carey group": "Careys construction",
    "ferrovial construccion": "Ferrovial",
    "tbi holdings": "Mobilis TBI",
}

# Rows whose feed is already covered by a better-performing existing query.
# "ACS / Dragados" is the Europe sheet's name for the group whose news the
# existing "Dragados" entry already carries (48 entries vs 0).
DROP = {"ACS / Dragados"}

# Punctuation in a company name silently degrades the Google News query.
# Measured 2026-08-22: "Byrne Group (Byrne Bros)" returns 0 entries and
# "Byrne Bros" returns 3; "Careys / Carey Group" returns 0 and "Carey Group"
# returns 9. So parentheticals are dropped and slash-alternatives are cut to
# the first form. NAME_FIXES overrides that default where the trimmed name
# still returns nothing — "JRL Group (Midgard)" trims to "JRL Group" which is
# right (10 entries), but "Midgard" alone returns 0.
NAME_FIXES = {"JRL Group (Midgard)": "JRL Group",
              "Byrne Group (Byrne Bros)": "Byrne Bros"}


def clean_name(name: str) -> str:
    """Query-safe trading name: no parentheticals, no slash alternatives."""
    if name in NAME_FIXES:
        return NAME_FIXES[name]
    name = re.sub(r"\s*\([^)]*\)", "", name)
    name = name.split(" / ")[0]
    return re.sub(r"\s+", " ", name).strip()


# Surname particles and initials — too common to be an identity hint, so the
# near-duplicate heuristic skips them. Without this, "J Coffey Construction"
# reads as a duplicate of "J Murphy & Sons" and "Van Oord" of "Van Berlo".
PARTICLES = {"j", "van", "de", "der", "den", "la", "le", "el", "di", "st"}

# Google News gl/ceid country codes for the document's country strings.
COUNTRY_CODE = {
    "France": "FR", "Spain": "ES", "Germany": "DE", "Austria": "AT",
    "Austria/Germany": "AT", "Switzerland": "CH", "Italy": "IT",
    "Netherlands": "NL", "Belgium": "BE", "Sweden": "SE", "Norway": "NO",
    "Finland": "FI", "Denmark": "DK", "Estonia": "EE", "Poland": "PL",
    "Czechia": "CZ", "Slovakia": "SK", "Hungary": "HU", "Romania": "RO",
    "Portugal": "PT", "Greece": "GR", "Ireland": "IE", "Turkiye": "TR",
}

# HQ state -> us_west, for NEW US entries only. The 20 US entries already in
# the watchlist keep whatever region they have: three of them (McCarthy MO,
# Mortenson MN, FlatironDragados GA) sit in us_west despite central or
# eastern HQs, so they were clearly assigned on where the work lands rather
# than on the head office, and that judgement isn't reproducible from a
# spreadsheet.
WEST_STATES = {"CA", "WA", "OR", "AK", "HI", "ID", "MT", "WY", "NV", "UT",
               "AZ", "CO", "NM", "TX", "OK", "KS", "NE", "SD", "ND"}


def _fold(text: str) -> str:
    """Lowercase, strip accents, drop punctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def norm(name: str) -> tuple[str, ...]:
    """Identity key: significant tokens, order-independent."""
    folded = _fold(ALIASES.get(_fold(name).strip(), name))
    return tuple(sorted(t for t in folded.split() if t and t not in NOISE))


def first_token(name: str) -> str:
    toks = [t for t in _fold(name).split()
            if t and t not in NOISE and t not in PARTICLES]
    return toks[0] if toks else ""


def target_region(rec: dict) -> str:
    if rec["sheet"] == "UK":
        return "uk"
    if rec["sheet"] == "Europe":
        return "eu"
    state = (rec["country"].rsplit(",", 1)[-1]).strip().upper()[:2]
    return "us_west" if state in WEST_STATES else "us_east"


def locale(rec: dict) -> dict:
    """English-language locale, geographically targeted. UK and US entries
    use the region defaults in news.REGION_LOCALE, so they need no override."""
    if rec["sheet"] != "Europe":
        return {}
    cc = COUNTRY_CODE.get(rec["country"])
    if not cc:
        return {}
    return {"hl": "en", "gl": cc, "ceid": f"{cc}:en"}


def included(rec: dict) -> bool:
    if rec["concrete_fit"] == "Low":
        return False
    return rec["tier"] == "1" or rec["concrete_fit"] == "High"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    with open(CSV_PATH, encoding="utf-8") as f:
        recs = list(csv.DictReader(f))

    wl = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
    existing = wl.get("contractors", [])
    # Existing keys are scoped by region: "Skanska UK" and "Skanska USA" are
    # genuinely different feeds, so a name only counts as covered if it is
    # already covered in the same region bucket.
    by_region: dict[str, set] = {}
    firsts: dict[str, dict[str, str]] = {}
    for e in existing:
        r = e.get("region", "uk")
        by_region.setdefault(r, set()).add(norm(e["name"]))
        firsts.setdefault(r, {})[first_token(e["name"])] = e["name"]

    def bucket(region: str) -> str:
        return "us" if region.startswith("us") else region

    covered_us = set().union(*(by_region.get(r, set())
                               for r in ("us", "us_east", "us_west"))) or set()
    us_firsts = {}
    for r in ("us", "us_east", "us_west"):
        us_firsts.update(firsts.get(r, {}))

    new, skipped, nearby, dropped = [], [], [], []
    for rec in recs:
        if not included(rec):
            dropped.append(rec)
            continue
        region = target_region(rec)
        # Key off the CLEANED name — that is what lands in the file, so a
        # second run must recognise its own previous output as covered.
        key = norm(clean_name(rec["company"]))
        if bucket(region) == "us":
            seen, fseen = covered_us, us_firsts
        else:
            seen, fseen = by_region.get(region, set()), firsts.get(region, {})
        if key in seen:
            skipped.append(rec)
            continue
        ft = first_token(clean_name(rec["company"]))
        if ft in fseen:
            nearby.append((rec, fseen[ft]))
        if rec["company"] in DROP:
            skipped.append(rec)
            continue
        entry = {"name": clean_name(rec["company"]), "region": region,
                 **locale(rec)}
        new.append((rec, entry))
        seen.add(key)

    print(f"Document rows            : {len(recs)}")
    print(f"  excluded by fit/tier   : {len(dropped)}")
    print(f"  already in watchlist   : {len(skipped)}")
    print(f"  new entries            : {len(new)}")
    from collections import Counter
    print("  new by region          :", dict(Counter(e["region"] for _, e in new)))
    print(f"\nWatchlist feeds: {len(existing)} -> {len(existing) + len(new)}")

    if nearby:
        print(f"\n{len(nearby)} possible near-duplicates — kept as new, "
              f"review and delete either side if they are the same feed:")
        for rec, other in nearby:
            print(f"  doc '{rec['company']}' ({target_region(rec)}) "
                  f"vs existing '{other}'")

    low = [r for r in dropped if r["concrete_fit"] == "Low"]
    print(f"\nExcluded for Low concrete fit ({len(low)}):")
    for r in low:
        print(f"  [{r['sheet']} T{r['tier']}] {r['company']} — {r['sectors']}")
    med = [r for r in dropped if r["concrete_fit"] != "Low"]
    print(f"\nExcluded as Tier 2 / Medium fit ({len(med)}) — "
          f"add by relaxing included()")

    block = render(new)
    if args.write:
        splice(block, len(new))
        print(f"\nWrote {len(new)} entries into watchlist.yaml")
    else:
        out = ROOT / "watchlist_additions.yaml"
        out.write_text(block, encoding="utf-8")
        print(f"\nYAML block written to {out} (not spliced — pass --write)")
    return 0


def render(new: list) -> str:
    """Group the new entries into commented blocks matching the file's style."""
    groups: dict[tuple, list] = {}
    for rec, entry in new:
        if rec["sheet"] == "UK":
            head = ("uk", f"UK Tier {rec['tier']}")
        elif rec["sheet"] == "Europe":
            head = ("eu", rec["country"])
        else:
            head = (entry["region"], f"US Tier {rec['tier']}")
        groups.setdefault(head, []).append((rec, entry))

    lines = []
    for (region, label), items in groups.items():
        lines.append(f"\n  # ── {label} ({region}) "
                     f"{'─' * max(1, 54 - len(label) - len(region))}")
        width = max(len(i[1]["name"]) for i in items) + 3
        for rec, entry in items:
            name = f'"{entry["name"]}",'
            extra = ""
            if "hl" in entry:
                extra = (f', hl: {entry["hl"]}, gl: {entry["gl"]}, '
                         f'ceid: "{entry["ceid"]}"')
            lines.append(f'  - {{ name: {name:<{width}} '
                         f'region: {entry["region"]}{extra} }}')
    return "\n".join(lines) + "\n"


def splice(block: str, count: int) -> None:
    path = ROOT / "watchlist.yaml"
    text = path.read_text(encoding="utf-8")
    marker = "\n  # ── Canada Tier 1s"
    if marker not in text:
        raise SystemExit("anchor comment not found — splice manually")
    head, tail = text.split(marker, 1)
    banner = ("\n  # ══ Added from data/gc_tiers_uk_eu_us_2026-08-22.csv "
              "(Tier 1 + Tier 2 High) ══")
    path.write_text(head.rstrip("\n") + "\n" + banner + block
                    + marker + tail, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
