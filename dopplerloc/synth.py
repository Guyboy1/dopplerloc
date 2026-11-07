"""Synthetic world for end-to-end testing: fake ADS-B, decoys, and audio with real Doppler physics."""
import numpy as np, pandas as pd
from .geo import offset_latlon, lla_to_ecef
from .tracks import Track
from .model import emission_time, interp_pos, C_SOUND

LAT0, LON0 = 27.9506, -82.4572     # arbitrary observer location
from datetime import datetime, timezone
BASE = datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp()   # the synthetic world happens on this (UTC) day


def _flight(icao, e0, n0, alt, heading, speed, turn=0.0, accel=0.0, t_cpa=0.0, span=420, jet=False):
    """Integrate a track forward/back from (e0,n0) at s=0. Returns raw 5 s ADS-B rows (lat/lon/alt_m/time)."""
    s = np.arange(-span, span + 1, 1.0)
    out = {}
    for sign in (+1, -1):
        e, n, h, v = e0, n0, np.radians(heading), speed
        for k in range(0, span + 1):
            out[sign * k] = (e, n)
            h += sign * np.radians(turn) * 1.0
            v += sign * accel
            e += sign * v * np.sin(h)
            n += sign * v * np.cos(h)
    ss = np.array(sorted(out))
    E = np.array([out[k][0] for k in ss]); N = np.array([out[k][1] for k in ss])
    lat, lon = offset_latlon(LAT0, LON0, N, E)
    df = pd.DataFrame({"icao24": icao, "time": t_cpa + ss, "lat": lat, "lon": lon, "alt_m": alt, "is_jet": jet})
    return df[(df.time % 5) == 0] if False else df.iloc[::5]


def world_rows(lat0=None, lon0=None):
    """The synthetic flights, placed around (lat0, lon0) (default: the module origin)."""
    global LAT0, LON0
    saved = (LAT0, LON0)
    if lat0 is not None:
        LAT0, LON0 = lat0, lon0
    try:
        return _world_rows()
    finally:
        LAT0, LON0 = saved


def _world_rows():
    TA, GAP = BASE + 10_000.0, 920.0
    rows = [
        # TRUE flights: gentle turns and speed changes, like real life
        _flight("trueA", 0, 800, 500, 90, 70, turn=0.04, accel=0.02, t_cpa=TA),
        _flight("trueB", -1100, 0, 700, 0, 60, turn=-0.05, accel=-0.015, t_cpa=TA + GAP),
        # decoy pair 1 at (0, 4800): perfect-looking geometry but a jet passes during the silence -> silence filter kills it
        _flight("decoyA1", 0, 5600, 600, 90, 66, t_cpa=TA),
        _flight("decoyB1", -1100, 4800, 800, 0, 64, t_cpa=TA + GAP),
        _flight("jetSilence", 0, 5600, 800, 90, 200, t_cpa=TA + 460, jet=True),
        # decoy pair 2 elsewhere: straight/constant, no silence breaker -> scoring has to reject it
        _flight("decoyA2", 8000, -5200, 450, 90, 67, t_cpa=TA),
        _flight("decoyB2", 6900, -6000, 650, 0, 63, t_cpa=TA + GAP),
        # unrelated clutter
        _flight("noise1", 3000, -9000, 800, 45, 55, t_cpa=TA + 200),
        _flight("noise2", -7000, 7000, 900, 200, 80, t_cpa=TA + 700),
    ]
    raw = pd.concat(rows, ignore_index=True)
    raw["time"] = raw["time"].astype(float)
    raw["typecode"] = np.where(raw.is_jet, "B738", "C172")
    return raw


def scenario(seed=0, noise=0.7, fs=2000):
    rng = np.random.default_rng(seed)
    TA, GAP = BASE + 10_000.0, 920.0
    raw = _world_rows()

    # --- audio from the TRUE flights only, observer at (LAT0, LON0) ---
    obs = lla_to_ecef(LAT0, LON0, 2.0)
    full = {}
    for ic in ("trueA", "trueB"):
        g = raw[raw.icao24 == ic]
        tt = np.arange(g.time.iloc[0], g.time.iloc[-1], 1.0)
        pos = lla_to_ecef(np.interp(tt, g.time, g.lat), np.interp(tt, g.time, g.lon), np.interp(tt, g.time, g.alt_m))
        full[ic] = Track(ic, tt, pos)
    from .model import cpa_points
    cA = cpa_points(full["trueA"], obs[None], 5000)
    T0 = cA.t[0] - 120.0                         # audio clock: starts 120 s before event 1
    dur = 120 + GAP + 150
    n = int(dur * fs)
    x = rng.normal(0, noise, n).astype(np.float32)
    th_all = T0 + np.arange(n) / fs
    for ic, f0 in (("trueA", 95.0), ("trueB", 142.0)):
        tr = full[ic]
        c = cpa_points(tr, obs[None], 5000)
        sel = np.abs(th_all - c.t[0]) < 100
        th = th_all[sel]
        te = emission_time(tr, obs, th)
        R = np.linalg.norm(interp_pos(tr, te) - obs, axis=-1)
        gate = 0.5 * (1 + np.cos(np.pi * np.clip(np.abs(th - c.t[0]) / 100, 0, 1)))
        sig = sum(a * np.sin(2 * np.pi * k * f0 * te) for k, a in ((1, 1.0), (2, 0.7), (3, 0.4)))
        x[sel] += (gate * 900.0 / R * sig).astype(np.float32)
    truth = dict(T0=T0, lat=LAT0, lon=LON0, tauA=cA.t[0] - T0, tauB=cA.t[0] - T0 + GAP, jets={"jetSilence"})
    return raw, x.astype(np.float32), fs, truth
