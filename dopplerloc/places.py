"""Country -> state -> city picker backed by bundled data (see tools/build_data.py for sources)."""
import gzip, json, math
from collections import Counter
from functools import lru_cache
from pathlib import Path
import numpy as np

_DATA = Path(__file__).parent / "data" / "places.json.gz"

# Hand-set (south, west, north, east) for countries whose city-derived box would be misleading
# (overseas territories etc.). Approximate; the UI lets you edit the box.
COUNTRY_BBOX = {
    "US": (24.4, -125.0, 49.5, -66.9),    # contiguous US; pick Alaska / Hawaii as states instead
    "CA": (41.6, -141.0, 70.0, -52.6), "AU": (-44.0, 112.9, -10.0, 153.7), "NZ": (-47.4, 166.3, -34.3, 178.6),
    "GB": (49.8, -8.7, 60.9, 1.8), "IE": (51.3, -10.6, 55.5, -5.9), "DE": (47.2, 5.8, 55.1, 15.1),
    "FR": (41.3, -5.2, 51.2, 9.7), "ES": (35.9, -9.4, 43.9, 3.4), "IT": (36.6, 6.6, 47.1, 18.6),
    "NL": (50.7, 3.3, 53.6, 7.3), "JP": (24.0, 122.9, 45.6, 146.0), "BR": (-33.8, -74.0, 5.3, -34.8),
    "IN": (6.7, 68.1, 35.7, 97.4), "ZA": (-34.9, 16.4, -22.1, 32.9),
    "RU": (41.2, 19.6, 77.0, 180.0),      # dataset box wraps the antimeridian
}


@lru_cache(maxsize=1)
def _db():
    with gzip.open(_DATA, "rt", encoding="utf8") as f:
        d = json.load(f)
    d["states_by_id"] = {s["id"]: s for s in d["states"]}
    return d


@lru_cache(maxsize=1)
def _usable():
    """Countries we can actually build a search box for (drops Antarctica, uninhabited and obsolete codes)."""
    out = []
    for c in _db()["countries"]:
        if c["c"] in ("UM", "AQ"):
            continue
        try:
            resolve(c["c"])
            out.append(c)
        except KeyError:
            pass
    return out


@lru_cache(maxsize=1)
def _verified():
    """Codes that passed tools/verify_countries.py (the full pipeline run at the country's largest city)."""
    f = Path(__file__).parent / "data" / "supported_countries.json"
    if f.exists():
        return set(json.load(open(f, encoding="utf8"))["supported"])
    return None


def countries():
    v = _verified()
    from .coverage import country_level
    return [{"code": c["c"], "name": c["n"], "coverage": country_level(c["c"])[0]} for c in _usable() if v is None or c["c"] in v]


_ALIASES = {"usa": "US", "united states of america": "US", "u.s.": "US", "u.s.a.": "US", "uk": "GB", "united kingdom": "GB",
            "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "south korea": "KR", "korea": "KR",
            "russia": "RU", "czechia": "CZ", "turkiye": "TR", "türkiye": "TR", "uae": "AE"}


def match_hint(country=None, state=None, city=None):
    """Free-text place names found in metadata -> {'country': code, 'state_id': id, 'city': name} (whatever can be matched)."""
    d = _db()
    out = {}
    if country:
        c = country.strip().lower()
        code = _ALIASES.get(c) or next((x["c"] for x in d["countries"] if x["n"].lower() == c or x["c"].lower() == c), None)
        if code:
            out["country"] = code
    cc = out.get("country")
    if state and cc:
        s = state.strip().lower()
        st = next((x for x in d["states"] if x["c"] == cc and (x["n"].lower() == s)), None)
        if st:
            out["state_id"] = st["id"]
    if city:
        cl = city.strip().lower()
        rows = [x for x in d["cities"] if x[0].lower() == cl and (cc is None or x[6] == cc)]
        if rows:
            best = max(rows, key=lambda r: r[5])
            out.setdefault("country", best[6])
            if "state_id" not in out and best[4] is not None:
                out["state_id"] = best[4]
            out["city"] = best[0]
    return out


def states(cc):
    return sorted(({"id": s["id"], "name": s["n"]} for s in _db()["states"] if s["c"] == cc), key=lambda s: s["name"])


def cities(cc, state_id=None, limit=400):
    rows = [c for c in _db()["cities"] if c[6] == cc and (state_id is None or c[4] == state_id)]
    rows.sort(key=lambda c: -c[5])
    return [{"name": c[0], "lat": c[1], "lon": c[2], "tz": c[3], "pop": c[5]} for c in rows[:limit]]


def _pad(b, deg):
    return (max(b[0] - deg, -90), max(b[1] - deg, -180), min(b[2] + deg, 90), min(b[3] + deg, 180))


@lru_cache(maxsize=1)
def _extra():
    with open(Path(__file__).parent / "data" / "country_extra.json", encoding="utf8") as f:
        return json.load(f)


def _tzs(rows, cc=None):
    """Zones seen in these cities (most common first), then the rest of the country's IANA zones."""
    out = [t for t, _ in Counter(r[3] for r in rows).most_common()]
    for t in _extra()["tz"].get(cc, []) if cc else []:
        if t not in out:
            out.append(t)
    return out


def _country_box(cc, crow):
    """(bbox, note): hand-set box > Natural Earth box > estimate from cities > from states."""
    if cc in COUNTRY_BBOX:
        return COUNTRY_BBOX[cc], "national box (mainland); pick a state for overseas parts"
    ne = _extra()["bbox"].get(cc)
    if ne and ne[3] - ne[1] < 300 and ne[2] - ne[0] > 0:       # skips boxes that wrap the antimeridian
        return _pad(tuple(ne), 0.2), "national box (Natural Earth); check/adjust"
    if crow:
        la = np.array([c[1] for c in crow]); lo = np.array([c[2] for c in crow])
        b = _pad((np.percentile(la, 1), np.percentile(lo, 1), np.percentile(la, 99), np.percentile(lo, 99)), 1.0)
        return tuple(float(v) for v in b), "estimated from city locations; check/adjust the box"
    st = [s for s in _db()["states"] if s["c"] == cc]
    if st:
        return _pad((min(s["lat"] for s in st), min(s["lon"] for s in st), max(s["lat"] for s in st), max(s["lon"] for s in st)), 1.5), \
               "estimated from regions; check/adjust the box"
    raise KeyError(f"no place data for country {cc}")


def resolve(cc, state_id=None, city=None):
    """-> dict(bbox=(S,W,N,E), timezones=[...], level='city|state|country', note=str)."""
    d = _db()
    crow = [c for c in d["cities"] if c[6] == cc]
    if city and city != "":
        m = [c for c in crow if c[0] == city and (state_id in (None, "") or c[4] == int(state_id))]
        if m:
            c = max(m, key=lambda r: r[5])
            return dict(bbox=_pad((c[1], c[2], c[1], c[2]), 0.25), timezones=_tzs([c], cc), level="city",
                        note="about 55 km square around the city centre")
    if state_id not in (None, ""):
        sr = [c for c in crow if c[4] == int(state_id)]
        st = d["states_by_id"][int(state_id)]
        lats = [c[1] for c in sr] + [st["lat"]]
        lons = [c[2] for c in sr] + [st["lon"]]
        span = max(max(lats) - min(lats), max(lons) - min(lons))
        b = _pad((min(lats), min(lons), max(lats), max(lons)), max(0.6, 0.2 * span))
        return dict(bbox=b, timezones=_tzs(sr or crow, cc), level="state",
                    note="estimated from the state's cities; check/adjust the box")
    box, note = _country_box(cc, crow)
    return dict(bbox=tuple(box), timezones=_tzs(crow, cc), level="country", note=note)


def nearest(lat, lon):
    """Reverse lookup for GPS metadata -> country/state/city (nearest listed city)."""
    d = _db()
    la = np.array([c[1] for c in d["cities"]]); lo = np.array([c[2] for c in d["cities"]])
    dist = (la - lat) ** 2 + ((lo - lon) * math.cos(math.radians(lat))) ** 2
    i = int(np.argmin(dist))
    c = d["cities"][i]
    st = d["states_by_id"].get(c[4])
    return dict(country=c[6], state_id=c[4], state=st["n"] if st else None, city=c[0], tz=c[3],
                km=float(np.sqrt(dist[i]) * 111.32))
