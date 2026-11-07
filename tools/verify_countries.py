"""Which countries does the pipeline really work for?  For every country in the place data this runs the full
two-curve search on a synthetic world placed at the country's largest city (grid maths, time zone, box, ECEF) and keeps
the countries that recover the true spot to < 100 m. Writes dopplerloc/data/supported_countries.json.
(It cannot know whether ADS-B data EXISTS for a country; that depends on your data source.)
Usage: python tools/verify_countries.py"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from dopplerloc import places, synth
from dopplerloc.audio import make_spec, fit_event
from dopplerloc.search import locate, Params
from dopplerloc.tracks import build_tracks
from dopplerloc.timeframe import Timeframe
from dopplerloc.geo import M_PER_DEG
from zoneinfo import ZoneInfo

raw0, x, fs, truth = synth.scenario()
spec = make_spec(x, fs)
e1, e2 = fit_event(spec, (60, 450), (35, 212)), fit_event(spec, (60, 450), (953, 1131))
db = places._db()
ok, bad = [], {}
t0 = time.time()
codes = [c["c"] for c in db["countries"] if c["c"] not in ("UM", "AQ")]
for n, cc in enumerate(codes, 1):
    try:
        r = places.resolve(cc)
        tz = r["timezones"][0]
        ZoneInfo(tz)
        Timeframe(2026, 11, tz, day=3, exact="12:00:00").windows()
        cities = sorted((c for c in db["cities"] if c[6] == cc), key=lambda c: -c[5])
        if cities:
            lat, lon = cities[0][1], cities[0][2]
        else:
            b = r["bbox"]; lat, lon = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if abs(lon) > 178:                      # a box centred on the antimeridian is not searchable as one box
            raise ValueError("searches cannot straddle the antimeridian")
        tracks = build_tracks(synth.world_rows(lat, lon))
        bbox = (lat - .12, lon - .12, lat + .12, lon + .12)
        res, _ = locate(tracks, spec, e1, e2, bbox, p=Params(), t0_window=(truth["T0"] - 30, truth["T0"] + 30), log=lambda m: None)
        top = res.iloc[0]
        err = float(np.hypot((top.lat - lat) * M_PER_DEG, (top.lon - lon) * M_PER_DEG * np.cos(np.radians(lat))))
        if err < 100 and top.flight1.startswith("trueA"):
            ok.append(cc)
        else:
            bad[cc] = f"search error {err:.0f} m"
    except Exception as e:
        bad[cc] = f"{type(e).__name__}: {e}"[:120]
    if n % 20 == 0:
        print(f"{n}/{len(codes)} done, {len(ok)} ok, {time.time()-t0:.0f}s", flush=True)
json.dump(dict(supported=sorted(ok), failed=bad, note="verified by tools/verify_countries.py (synthetic world at the largest city)"),
          open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dopplerloc", "data", "supported_countries.json"), "w"), indent=1)
print("FINISHED", len(ok), "supported;", len(bad), "failed", flush=True)
