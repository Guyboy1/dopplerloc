import sys, numpy as np
sys.path.insert(0, "/home/claude/dopplerloc")
from dopplerloc.synth import scenario
from dopplerloc.tracks import build_tracks
from dopplerloc.audio import make_spec, detect_events, fit_event
from dopplerloc.search import locate, Params
from dopplerloc.geo import M_PER_DEG
for noise in (0.7, 2.0, 4.0):
    for seed in range(3):
        raw, x, fs, truth = scenario(seed=seed, noise=noise)
        tracks = build_tracks(raw); spec = make_spec(x, fs)
        try:
            w = sorted(detect_events(spec)[:2]); evs = [fit_event(spec, (60, 450), (a, b)) for a, b, _ in w]
            bbox = (truth["lat"]-.12, truth["lon"]-.12, truth["lat"]+.12, truth["lon"]+.12)
            res, _ = locate(tracks, spec, evs[0], evs[1], bbox, p=Params(), log=lambda *a: None)
            t = res.iloc[0]
            err = np.hypot((t.lat-truth["lat"])*M_PER_DEG, (t.lon-truth["lon"])*M_PER_DEG*np.cos(np.radians(truth["lat"])))
            print(f"noise={noise} seed={seed} top={t.flight1}+{t.flight2} err={err:.0f}m margin={t.score-res.score.iloc[1]:.1f}")
        except Exception as e:
            print(f"noise={noise} seed={seed} FAILED: {e}")
