import sys, time, numpy as np
sys.path.insert(0, "/home/claude/dopplerloc")
from dopplerloc.synth import scenario
from dopplerloc.tracks import build_tracks
from dopplerloc.audio import make_spec, detect_events, fit_event
from dopplerloc.search import locate, Params
from dopplerloc.geo import M_PER_DEG

raw, x, fs, truth = scenario()
tracks = build_tracks(raw)
print("tracks:", len(tracks), "jets:", [t.id for t in tracks if t.is_jet])
spec = make_spec(x, fs)
ev_found = detect_events(spec)
print("auto events:", [(round(a), round(b)) for a, b, _ in ev_found[:3]])
print("truth tau:", round(truth["tauA"]), round(truth["tauB"]))
wins = sorted(ev_found[:2])
evs = [fit_event(spec, (60, 450), (a, b)) for a, b, _ in wins]
for e in evs: print(e)
bbox = (truth["lat"] - 0.12, truth["lon"] - 0.12, truth["lat"] + 0.12, truth["lon"] + 0.12)
t0 = time.time()
res, info = locate(tracks, spec, evs[0], evs[1], bbox, p=Params(), t0_window=(truth['T0']-30, truth['T0']+30))
print(res.head(6).to_string()); print("time", round(time.time() - t0, 1), "s")
top = res.iloc[0]
err = np.hypot((top.lat - truth["lat"]) * M_PER_DEG, (top.lon - truth["lon"]) * M_PER_DEG * np.cos(np.radians(truth["lat"])))
print(f"TOP: {top.flight1}+{top.flight2} err={err:.0f} m  margin={top.score - res.score.iloc[1] if len(res)>1 else float('nan'):.1f}")
