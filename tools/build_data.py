"""Build dopplerloc/data/places.json.gz and typecodes.csv from public datasets.
Sources: dr5hn/countries-states-cities-database (states, ODbL), GeoNames via `geonamescache` (cities, CC-BY 4.0),
Berkanktk/datasets all_aircrafts.csv (ICAO Doc 8643 engine types).
Usage: python tools/build_data.py states.json all_aircrafts.csv bounding-boxes.json
Country boxes: sandstrom/country-bounding-boxes (Natural Earth, public domain data)."""
import sys, json, gzip, csv, math, collections
import geonamescache

states_path, ac_path = sys.argv[1], sys.argv[2]
gc = geonamescache.GeonamesCache()
countries = {k: v["name"] for k, v in gc.get_countries().items()}
raw_states = [s for s in json.load(open(states_path, encoding="utf8")) if s.get("parent_id") in (None, "")]
states, by_cc = [], collections.defaultdict(list)
for s in raw_states:
    try:
        lat, lon = float(s["latitude"]), float(s["longitude"])
    except (TypeError, ValueError):
        continue
    st = dict(id=len(states), c=s["country_code"], n=s["name"], lat=round(lat, 3), lon=round(lon, 3))
    keys = {str(s.get("iso2") or "").upper(), str(s.get("fips_code") or "").upper()} - {""}
    st["_keys"] = keys
    states.append(st); by_cc[st["c"]].append(st)

cities, matched = [], 0
for c in gc.get_cities().values():
    cc = c["countrycode"]; cand = by_cc.get(cc, [])
    sid = None
    a1 = str(c.get("admin1code") or "").upper()
    for st in cand:
        if a1 and a1 in st["_keys"]:
            sid = st["id"]; matched += 1; break
    if sid is None and cand:       # fall back to nearest state centroid in the same country
        sid = min(cand, key=lambda st: (st["lat"] - c["latitude"]) ** 2 + ((st["lon"] - c["longitude"]) * math.cos(math.radians(c["latitude"]))) ** 2)["id"]
    cities.append([c["name"], round(c["latitude"], 4), round(c["longitude"], 4), c["timezone"], sid, c["population"], cc])
print("cities", len(cities), "matched by code", matched)
for st in states:
    st.pop("_keys")
out = dict(countries=[dict(c=k, n=v) for k, v in sorted(countries.items(), key=lambda kv: kv[1])], states=states, cities=cities)
with gzip.open("dopplerloc/data/places.json.gz", "wt", encoding="utf8") as f:
    json.dump(out, f, separators=(",", ":"), ensure_ascii=False)

# typecode -> engine type (P piston, T turboprop/shaft, J jet, E electric, R rocket)
eng = {}
with open(ac_path, encoding="utf8", newline="") as f:
    for r in csv.DictReader(f):
        code, e = r["icao"].strip(), r["engine_type"].strip()
        if code and e and code not in eng:
            eng[code] = (e, r.get("class", "").strip())
with open("dopplerloc/data/typecodes.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["typecode", "engine", "class"])
    for k, (e, c) in sorted(eng.items()): w.writerow([k, e, c])
print("typecodes", len(eng))

# country boxes (sandstrom/country-bounding-boxes, Natural Earth) + time zones per country (IANA zone.tab)
if len(sys.argv) > 3:
    cbb = json.load(open(sys.argv[3], encoding="utf8"))
    tz = {}
    for line in open("/usr/share/zoneinfo/zone.tab", encoding="utf8"):
        if line.startswith("#") or not line.strip():
            continue
        cc, _, zone = line.split("\t")[:3]
        tz.setdefault(cc, []).append(zone.strip())
    json.dump(dict(bbox={k: [v[1][1], v[1][0], v[1][3], v[1][2]] for k, v in cbb.items()}, tz=tz),
              open("dopplerloc/data/country_extra.json", "w"), separators=(",", ":"))
