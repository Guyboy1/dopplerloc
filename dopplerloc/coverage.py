"""Flight-data coverage hints (advisory only; nothing here blocks a search).

Community ADS-B archives (adsb.lol, OpenSky's ground receivers) depend on volunteer feeders, so coverage varies a lot.
Levels: "moderate" (partial; localised around big airports/cities, mostly high-altitude traffic) and
"low" (little or no ground-receiver data).  Anything not listed gets level "none" (no warning; not a guarantee).
The lists come from the project owner's own assessment; they are not measured.  Edit them freely.
"""

REASON = {
    "asia_east": "Receivers are clustered around big airports and cities; coverage at ground level is sparse, mainly high-altitude traffic.",
    "asia_south": "Few volunteer receivers relative to the traffic; coverage is patchy and mostly high-altitude.",
    "mideast": "Coverage exists near major airports and cities; low-level coverage is thin and some airspace is restricted or not shared.",
    "latam": "Feeders are concentrated in big cities (especially the south-east of Brazil); elsewhere coverage is patchy and low-altitude tracks are often missing.",
    "latam_low": "Very few volunteer receivers and limited infrastructure; coverage is patchy even around the main cities.",
    "africa": "Receivers exist mainly around major hub airports; away from them coverage is sparse.",
    "africa_low": "Very few receivers and little infrastructure; coverage is patchy, mostly high-altitude overflights, and often missing at ground level.",
    "china": "Coverage is partial, best in the east near big cities and airports; much less in the interior and west.",
    "russia": "Receivers are mainly in the west (Moscow, St Petersburg) and around a few big cities; most of the country is thinly covered.",
    "sea_low": "Few receivers and limited feeder networks; only a handful of airports have any ground-level coverage.",
    "conflict": "Conflict, closed airspace or restrictions: few receivers, little civil traffic, and some flights are not reported.",
    "central_asia": "Very few receivers over large, thinly populated land; ground-level tracks are largely missing.",
    "steppe": "Very few receivers over vast, thinly populated land, so ground-level tracks are largely missing.",
    "pacific": "Rugged, remote terrain with very few receivers; only a few airports are covered.",
}

# country code -> (level, reason key)
COUNTRY = {}
def _set(level, key, codes):
    for _c in codes.split():
        COUNTRY[_c] = (level, key)

# ---- moderate: coverage exists but is localised around major airports/cities, mostly high-altitude
_set("moderate", "asia_east", "JP KR TW SG MY TH PH ID VN KH")
_set("moderate", "china", "CN")
_set("moderate", "asia_south", "IN PK BD LK NP")
_set("moderate", "mideast", "AE SA QA IL BH KW JO LB OM")
_set("moderate", "latam", "BR AR CL CO MX PE EC UY CR PA GT DO SV HN NI JM")
_set("moderate", "africa", "ZA EG KE MA NG GH ET TZ UG DZ TN SN CI CM ZM ZW MZ NA BW AO RW")
_set("moderate", "russia", "RU BY")
# ---- low: little or no ground coverage
_set("low", "conflict", "AF SS SO YE SY LY SD IQ IR UA")
_set("low", "africa_low", "TD NE ML MR CF CD CG ER BF GN GW SL LR BI MG GA GQ DJ KM LS SZ MW TG BJ GM")
_set("low", "central_asia", "TM UZ TJ KG")
_set("low", "steppe", "KZ MN")
_set("low", "sea_low", "MM LA KP BT")
_set("low", "latam_low", "VE BO PY CU HT GY SR")
_set("low", "pacific", "PG")

# Regions that override the country level (south, west, north, east) - rough boxes
ZONES = [
    ("Siberia (Russia)", (50.0, 60.0, 77.0, 180.0), "low",
     "Vast, thinly populated area with very few ground receivers (a few big cities aside); satellite-relayed (ADS-C) data, mostly via OpenSky, exists only for some remote routes."),
    ("Sahara region", (16.0, -15.0, 29.0, 30.0), "low",
     "Desert with almost no receivers; air traffic overhead is high-altitude and mostly not captured by ground feeders."),
    ("Amazon basin", (-12.0, -74.0, 4.0, -50.0), "low",
     "Dense rainforest with very few receivers; low-level coverage is practically absent."),
]
LABEL = {"moderate": "Moderate coverage", "low": "Low coverage", "none": ""}


def _overlap_frac(b, z):
    s, w, n, e = max(b[0], z[0]), max(b[1], z[1]), min(b[2], z[2]), min(b[3], z[3])
    area = max(b[2] - b[0], 1e-9) * max(b[3] - b[1], 1e-9)
    return max(0.0, n - s) * max(0.0, e - w) / area


def country_level(cc):
    """('moderate'|'low'|'none', reason) for a whole country (used to tag the country list)."""
    if cc in COUNTRY:
        lv, key = COUNTRY[cc]
        return lv, REASON[key]
    return "none", ""


def assess(cc=None, bbox=None):
    """Coverage hint for a search box (S, W, N, E) in country `cc`.
    A listed zone wins if it holds at least half of the box; otherwise the country level applies."""
    if bbox is not None:
        b = tuple(float(v) for v in bbox)
        best = None
        for name, zb, lv, why in ZONES:
            f = _overlap_frac(b, zb)
            if f >= 0.5 and (best is None or f > best[0]):
                best = (f, name, lv, why)
        if best:
            return dict(level=best[2], label=LABEL[best[2]], area=best[1], reason=best[3])
    lv, why = country_level(cc)
    return dict(level=lv, label=LABEL[lv], area=None, reason=why)
