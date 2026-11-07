"""Scale check: the true flights hidden among thousands of random ones over a ~20x20 degree area."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import dopplerloc.synth as S
from dopplerloc.synth import scenario, LAT0, LON0, _flight
from dopplerloc.tracks import build_tracks
from dopplerloc.audio import make_spec, detect_events, fit_event
from dopplerloc.search import locate, Params
from dopplerloc.geo import M_PER_DEG

raw, x, fs, truth = scenario()
rng = np.random.default_rng(0); T0 = truth["T0"]; TA = S.BASE + 10000.0
extra = []
for i in range(3000):
    la = rng.uniform(LAT0 - 10, LAT0 + 10); lo = rng.uniform(LON0 - 10, LON0 + 10)
    S.LAT0, S.LON0 = la, lo                       # _flight builds around module LAT0/LON0
    extra.append(_flight(f"rnd{i}", 0, 0, rng.uniform(300, 1500), rng.uniform(0, 360), rng.uniform(45, 90),
                         turn=rng.normal(0, .05), t_cpa=TA + rng.uniform(-400, 1500), span=300))
S.LAT0, S.LON0 = LAT0, LON0
allraw = pd.concat([raw] + extra, ignore_index=True)
t = time.time(); tracks = build_tracks(allraw); print(f"{len(tracks)} tracks built in {time.time()-t:.1f}s")
spec = make_spec(x, fs); w = sorted(detect_events(spec)[:2]); evs = [fit_event(spec, (60, 450), (a, b)) for a, b, _ in w]
bbox = (LAT0 - 10, LON0 - 10, LAT0 + 10, LON0 + 10)
for workers in (1, 4):
    t = time.time()
    res, info = locate(tracks, spec, evs[0], evs[1], bbox, p=Params(workers=workers), t0_window=(T0 - 600, T0 + 600), log=lambda m: None)
    top = res.iloc[0]
    err = np.hypot((top.lat - LAT0) * M_PER_DEG, (top.lon - LON0) * M_PER_DEG * np.cos(np.radians(LAT0)))
    print(f"workers={workers}: {time.time()-t:.1f}s  tiles={info['tiles']} counts={info['counts']}  top={top.flight1}+{top.flight2} err={err:.0f} m "
          f"margin={(res.score.iloc[0]-res.score.iloc[1]) if len(res)>1 else float('nan'):.1f}")
