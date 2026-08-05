"""Geocoding via Nominatim (OpenStreetMap) — free, no key.

Policy compliance: identifying User-Agent, max 1 request/second, results
cached in state/geocache.json so each place is looked up once ever.
Precision is honest: town/region-level for most projects, so downstream
views should label locations as approximate.
"""
from __future__ import annotations

import time

import requests

from . import state

URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "007-radar/1.0 (Converge; internal project mapping)"}

_last_call = 0.0


def geocode(location: str, country: str = "") -> tuple[float, float] | None:
    """Return (lat, lng) or None. Cached; rate-limited to 1 rps."""
    global _last_call
    if not location:
        return None
    key = f"{location}|{country}".lower().strip()

    cache = state.load("geocache")
    if key in cache:
        hit = cache[key]
        return tuple(hit) if hit else None

    wait = 1.0 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()

    params = {"q": location, "format": "json", "limit": 1}
    if country:
        params["countrycodes"] = country.lower()
    try:
        resp = requests.get(URL, params=params, headers=HEADERS, timeout=30)
        results = resp.json() if resp.status_code == 200 else []
    except Exception:
        results = []

    coords = None
    if results:
        coords = (float(results[0]["lat"]), float(results[0]["lon"]))

    cache[key] = list(coords) if coords else None
    state.save("geocache", cache)
    return coords
