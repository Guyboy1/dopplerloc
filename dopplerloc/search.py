"""Country-scale search.

Narrowing pipeline (each step shrinks the work before the expensive scoring):
  1. time window   - only flights near the recording's possible time (and their heard-CPA must fit it)
  2. speed         - only samples of flights flying at the measured speed
  3. tiles         - the region is cut into ~2 degree tiles, processed independently (and in parallel)
  4. annulus       - per flight, candidate cells only in the ring at the measured CPA distance,
                     not a brute-force grid over the whole area
  5. offset filter - same cell must hear flight A then flight B the measured time apart
  6. silence filter- nobody else audible between the events
  7. scoring       - modelled Doppler curves vs the spectrogram, then fine refinement
"""
from dataclasses import dataclass, field
import concurrent.futures as cf
import numpy as np
import pandas as pd
from .geo import lla_to_ecef, offset_latlon, DEM, M_PER_DEG
from .model import C_SOUND, cpa_points, ratio_curve, ratio_many

KJ = 4_000_000
INF = float("inf")


@dataclass
class Params:
    c: float = C_SOUND
    grid_m: float = 200.0
    obs_h: float = 2.0              # microphone height above ground (m)
    speed_tol: float = 0.30         # +/- fraction around the measured speed
    dist_tol: float = 0.40          # +/- fraction around the measured CPA distance
    offset_tol_s: float = 8.0       # slack on the event-to-event time offset
    terrain_margin_m: float = 150.0 # unknown terrain relief when ringing candidates
    silence_prop_m: float = 3000.0  # a non-jet closer than this would have been heard
    silence_jet_m: float = 1500.0
    silence_pad_s: float = 10.0
    place_sweep_s: float = 6.0      # event-1 clock error sweep
    f_sweep_frac: float = 0.03      # rest-frequency sweep
    f_steps: int = 7
    mults: tuple = (1.0, 2.0)       # ridge + harmonics to score
    top_n: int = 20
    fine_top: int = 5               # refine the best N flight pairs
    fine_step_m: float = 25.0
    fine_radius_m: float = 150.0
    tile_deg: float = 2.0
    workers: int = 1
    max_rows_tile: int = 20000      # safety cap on (cell, flight pair) rows scored per tile
    tau_slack_s: float = 3.0        # curve-time measurement error when the clock is exact
    trace_scoring: bool = True      # hand-traced curves are scored by closeness to the traced line (video: faint curve)
    single_keep: int = 300          # one-curve mode: cells per tile kept after the cheap prescore


class Grid:
    """Global grid of ~step_m cells addressed by one int64 key (works across a whole country)."""
    def __init__(self, bbox, step_m):
        s, w, n, e = bbox
        self.step = step_m
        self.dlat = step_m / M_PER_DEG
        ref = 0.0 if s * n <= 0 else min(abs(s), abs(n))
        self.dlon = step_m / (M_PER_DEG * np.cos(np.radians(ref)))

    def key(self, lat, lon):
        return np.floor((np.asarray(lat) + 90) / self.dlat).astype(np.int64) * KJ + \
               np.floor((np.asarray(lon) + 180) / self.dlon).astype(np.int64)

    def latlon(self, key):
        key = np.asarray(key, np.int64)
        return ((key // KJ) + 0.5) * self.dlat - 90, ((key % KJ) + 0.5) * self.dlon - 180


def _rng(ev, p):
    return (ev.d * (1 - p.dist_tol), ev.d * (1 + p.dist_tol), ev.v * (1 - p.speed_tol), ev.v * (1 + p.speed_tol))


def _speeds(tr):
    dt = tr.t[1] - tr.t[0]
    return np.linalg.norm(np.gradient(tr.pos, dt, axis=0), axis=1)


def _narrow(tracks, ev, tw, p):
    """Flights with any sample at the right speed inside the time window; -> {id: (latmin,latmax,lonmin,lonmax)}"""
    _, _, vlo, vhi = _rng(ev, p)
    out = {}
    for tr in tracks:
        if tr.t[-1] < tw[0] - 60 or tr.t[0] > tw[1] + 60:
            continue
        v = _speeds(tr)
        m = (v >= vlo) & (v <= vhi) & (tr.t >= tw[0] - 60) & (tr.t <= tw[1] + 60)
        if m.any():
            out[tr.id] = (tr.lat[m].min(), tr.lat[m].max(), tr.lon[m].min(), tr.lon[m].max())
    return out


def _overlaps(b, tile, pad_deg):
    s, w, n, e = tile
    return not (b[1] < s - pad_deg or b[0] > n + pad_deg or b[3] < w - pad_deg or b[2] > e + pad_deg)


def _flight_cells(tr, ev, tw, grid, tile, dem, p):
    """Candidate cells for one flight: ring around the track at the measured CPA distance."""
    d_lo, d_hi, v_lo, v_hi = _rng(ev, p)
    dt = tr.t[1] - tr.t[0]
    v = _speeds(tr)
    m = (v >= v_lo) & (v <= v_hi) & (tr.t >= tw[0] - 60) & (tr.t <= tw[1] + 60)
    ts, tw_, tn, te = tile
    padlat = d_hi * 1.3 / M_PER_DEG
    padlon = padlat / max(np.cos(np.radians(max(abs(ts), abs(tn)))), 0.1)
    m &= (tr.lat >= ts - padlat) & (tr.lat <= tn + padlat) & (tr.lon >= tw_ - padlon) & (tr.lon <= te + padlon)
    idx = np.where(m)[0]
    if len(idx) == 0:
        return None
    stride = max(1, int(grid.step / (max(float(np.median(v[idx])), 20.0) * dt)))
    idx = idx[::stride]
    lat_s, lon_s, alt_s = tr.lat[idx], tr.lon[idx], tr.alt[idx]
    ground = dem(lat_s, lon_s) if dem.has_data else np.zeros(len(idx))
    margin = p.terrain_margin_m if dem.has_data else 50.0
    dh = np.abs(alt_s - (ground + p.obs_h))
    r_hi2 = d_hi ** 2 - np.maximum(dh - margin, 0) ** 2
    ok = r_hi2 > 0
    if not ok.any():
        return None
    lat_s, lon_s, dh, r_hi2 = lat_s[ok], lon_s[ok], dh[ok], r_hi2[ok]
    r_hi = np.sqrt(r_hi2)
    r_lo = np.sqrt(np.maximum(d_lo ** 2 - (dh + margin) ** 2, 0))
    cell_e = grid.dlon * M_PER_DEG * np.cos(np.radians(lat_s.mean()))
    Rn, Re = int(np.ceil(r_hi.max() / grid.step)) + 1, int(np.ceil(r_hi.max() / cell_e)) + 1
    DI, DJ = np.meshgrid(np.arange(-Rn, Rn + 1), np.arange(-Re, Re + 1), indexing="ij")
    DI, DJ = DI.ravel(), DJ.ravel()
    rho = np.hypot(DI * grid.step, DJ * cell_e)
    mask = (rho[None, :] >= (r_lo - grid.step)[:, None]) & (rho[None, :] <= (r_hi + grid.step)[:, None])
    ci0 = np.floor((lat_s + 90) / grid.dlat).astype(np.int64)
    cj0 = np.floor((lon_s + 180) / grid.dlon).astype(np.int64)
    keys = ((ci0[:, None] + DI[None, :]) * KJ + (cj0[:, None] + DJ[None, :]))[mask]
    keys = np.unique(keys)
    la, lo = grid.latlon(keys)
    inside = (la >= ts) & (la < tn) & (lo >= tw_) & (lo < te)      # each cell belongs to exactly one tile
    keys, la, lo = keys[inside], la[inside], lo[inside]
    if len(keys) == 0:
        return None
    P = lla_to_ecef(la, lo, dem(la, lo) + p.obs_h)
    r = cpa_points(tr, P, d_hi, p.c)
    k = (r.d >= d_lo) & (r.d <= d_hi) & (r.v >= v_lo) & (r.v <= v_hi) & (r.t >= tw[0]) & (r.t <= tw[1])
    if not k.any():
        return None
    return pd.DataFrame({"key": keys[r.pt[k]], "flight": tr.id, "t": r.t[k], "d": r.d[k], "v": r.v[k]})


def _trace_score(ev, F, T):
    """0..20 score for how closely a modelled curve F(T) follows the hand-traced line (only inside the traced span)."""
    tr = np.asarray(ev.trace, float)
    inspan = (T >= tr[0, 0]) & (T <= tr[-1, 0])
    ft = np.interp(T, tr[:, 0], tr[:, 1])
    ok = inspan & np.isfinite(F)
    d = np.where(ok, np.abs(F - ft), 0.0)
    n = np.maximum(ok.sum(-1), 1)
    pct = 100.0 * d.sum(-1) / n / ev.f_rest
    return 20.0 * np.exp(-pct / 1.5) * (ok.sum(-1) / np.maximum(inspan.sum(-1), 1))


class Scorer:
    def __init__(self, tracks, spec, ev1, ev2, dem, p):
        self.by_id = {t.id: t for t in tracks}
        self.spec, self.ev1, self.ev2, self.dem, self.p = spec, ev1, ev2, dem, p
        self.rel1 = np.arange(ev1.win[0] - ev1.tau, ev1.win[1] - ev1.tau + 1e-6, 0.5)
        self.rel2 = None if ev2 is None else np.arange(ev2.win[0] - ev2.tau, ev2.win[1] - ev2.tau + 1e-6, 0.5)
        self.deltas = np.arange(-p.place_sweep_s, p.place_sweep_s + 1e-6, 1.0)
        self.fr = 1 + np.linspace(-p.f_sweep_frac, p.f_sweep_frac, p.f_steps)

    def _event(self, ratio, rel, tau_place, f_rest, ev=None):
        T = tau_place[:, None, None] + rel[None, None, :]
        if ev is not None and ev.trace and self.p.trace_scoring:
            F, T = np.broadcast_arrays(f_rest[None, :, None] * ratio[None, None, :], T)
            return _trace_score(ev, F, T)
        tot = 0.0
        for m in self.p.mults:
            F = m * f_rest[None, :, None] * ratio[None, None, :]
            tot = tot + self.spec.sample(F, T).mean(-1)
        return tot / len(self.p.mults)

    def point(self, f1, f2, la, lo):
        p, ev1, ev2 = self.p, self.ev1, self.ev2
        tr1, tr2 = self.by_id[f1], self.by_id[f2]
        obs = lla_to_ecef(la, lo, float(self.dem(np.array([la]), np.array([lo]))[0]) + p.obs_h)
        c1 = cpa_points(tr1, obs[None], ev1.d * 3, p.c)
        c2 = cpa_points(tr2, obs[None], ev2.d * 3, p.c)
        if len(c1.t) == 0 or len(c2.t) == 0:
            return None
        ra1 = ratio_curve(tr1, obs, c1.t[0] + self.rel1, p.c)
        ra2 = ratio_curve(tr2, obs, c2.t[0] + self.rel2, p.c)
        s1 = self._event(ra1, self.rel1, ev1.tau + self.deltas, ev1.f_rest * self.fr, ev1)
        k = np.unravel_index(np.argmax(s1), s1.shape)
        tau2 = ev1.tau + self.deltas[k[0]] + (c2.t[0] - c1.t[0])        # event 2 LOCKED by flight geometry
        s2a = self._event(ra2, self.rel2, np.array([tau2]), ev2.f_rest * self.fr, ev2)[0]
        k2 = int(np.argmax(s2a))
        return (f1, f2, la, lo, s1[k], s2a[k2], c1.d[0], c2.d[0], float(self.deltas[k[0]]),
                float(ev1.f_rest * self.fr[k[1]]), float(ev2.f_rest * self.fr[k2]), float(c1.t[0]), float(c2.t[0]), float("nan"))


def _point_indep(self, f1, f2, la, lo):
    """Recording with cuts: each curve is pinned to its OWN real clock time, so nothing links their placements."""
    p, ev1, ev2 = self.p, self.ev1, self.ev2
    tr1, tr2 = self.by_id[f1], self.by_id[f2]
    obs = lla_to_ecef(la, lo, float(self.dem(np.array([la]), np.array([lo]))[0]) + p.obs_h)
    c1 = cpa_points(tr1, obs[None], ev1.d * 3, p.c)
    c2 = cpa_points(tr2, obs[None], ev2.d * 3, p.c)
    if len(c1.t) == 0 or len(c2.t) == 0:
        return None
    ra1 = ratio_curve(tr1, obs, c1.t[0] + self.rel1, p.c)
    ra2 = ratio_curve(tr2, obs, c2.t[0] + self.rel2, p.c)
    s1 = self._event(ra1, self.rel1, ev1.tau + self.deltas, ev1.f_rest * self.fr, ev1)
    s2 = self._event(ra2, self.rel2, ev2.tau + self.deltas, ev2.f_rest * self.fr, ev2)
    k1 = np.unravel_index(np.argmax(s1), s1.shape)
    k2 = np.unravel_index(np.argmax(s2), s2.shape)
    return (f1, f2, la, lo, s1[k1], s2[k2], c1.d[0], c2.d[0], float(self.deltas[k1[0]]),
            float(ev1.f_rest * self.fr[k1[1]]), float(ev2.f_rest * self.fr[k2[1]]), float(c1.t[0]), float(c2.t[0]),
            float(self.deltas[k2[0]]))


Scorer.point_indep = _point_indep


def _point1(self, f1, la, lo):
    """Single-event version of Scorer.point (used when the timeframe is specific enough for one curve)."""
    p, ev1 = self.p, self.ev1
    tr1 = self.by_id[f1]
    obs = lla_to_ecef(la, lo, float(self.dem(np.array([la]), np.array([lo]))[0]) + p.obs_h)
    c1 = cpa_points(tr1, obs[None], ev1.d * 3, p.c)
    if len(c1.t) == 0:
        return None
    ra1 = ratio_curve(tr1, obs, c1.t[0] + self.rel1, p.c)
    s1 = self._event(ra1, self.rel1, ev1.tau + self.deltas, ev1.f_rest * self.fr, ev1)
    k = np.unravel_index(np.argmax(s1), s1.shape)
    return (f1, "", la, lo, s1[k], 0.0, c1.d[0], 0.0, float(self.deltas[k[0]]),
            float(ev1.f_rest * self.fr[k[1]]), 0.0, float(c1.t[0]), float("nan"), float("nan"))


Scorer.point1 = _point1


def _prescore(tr, df, ev, grid, dem, spec, p):
    """Cheap vectorised score (no sweeps) for the (cell, flight) rows of one flight; used to keep only the
    most promising cells when only one curve is available. Heuristic: ranks, never rejects on its own."""
    la, lo = grid.latlon(df.key.to_numpy())
    P = lla_to_ecef(la, lo, dem(la, lo) + p.obs_h)
    rel = np.arange(ev.win[0] - ev.tau, ev.win[1] - ev.tau + 1e-6, 2.0)
    out = np.zeros(len(df))
    for a in range(0, len(df), 1500):
        sl = slice(a, a + 1500)
        ra = ratio_many(tr, P[sl], df.t.to_numpy()[sl, None] + rel[None, :], p.c)
        if ev.trace and p.trace_scoring:
            F, T = np.broadcast_arrays(ev.f_rest * ra, (ev.tau + rel)[None, :])
            out[sl] = _trace_score(ev, F, T)
            continue
        tot = 0.0
        for m in p.mults:
            tot = tot + spec.sample(m * ev.f_rest * ra, (ev.tau + rel)[None, :]).mean(1)
        out[sl] = tot / len(p.mults)
    return out


COLS = ["flight1", "flight2", "lat", "lon", "score1", "score2", "d1", "d2", "delta", "f1rest", "f2rest", "t1c", "t2c", "delta2"]
_CTX = None


def _init(ctx):
    global _CTX
    _CTX = ctx


def _empty_counts():
    return dict(cand1=0, cand2=0, offset=0, silence=0, scored=0, truncated=0)


def _tile_task(tile):
    c = _CTX
    p, grid, dem = c["p"], c["grid"], c["dem"]
    cnt = _empty_counts()
    pad = 3500 / M_PER_DEG
    if c["ev2"] is None:
        return _tile_single(tile, c, cnt, pad)
    if c["indep"]:
        return _tile_indep(tile, c, cnt, pad)
    f1 = [t for t in c["c1"] if _overlaps(c["bb1"][t.id], tile, pad)]
    f2 = [t for t in c["c2"] if _overlaps(c["bb2"][t.id], tile, pad)]
    if not f1 or not f2:
        return pd.DataFrame(columns=COLS), cnt
    rows1 = [x for x in (_flight_cells(t, c["ev1"], c["tw1"], grid, tile, dem, p) for t in f1) if x is not None]
    rows2 = [x for x in (_flight_cells(t, c["ev2"], c["tw2"], grid, tile, dem, p) for t in f2) if x is not None]
    if not rows1 or not rows2:
        return pd.DataFrame(columns=COLS), cnt
    t1, t2 = pd.concat(rows1, ignore_index=True), pd.concat(rows2, ignore_index=True)
    cnt["cand1"], cnt["cand2"] = len(t1), len(t2)

    m = t1.merge(t2, on="key", suffixes=("1", "2"))
    dev = (m.t2 - m.t1 - c["off"]).abs()
    m = m[(m.flight1 != m.flight2) & (dev <= p.offset_tol_s)].reset_index(drop=True)
    cnt["offset"] = len(m)
    if m.empty:
        return pd.DataFrame(columns=COLS), cnt

    # silence filter
    ev1, ev2 = c["ev1"], c["ev2"]
    q_lo, q_hi = ev1.win[1] - ev1.tau, ev2.win[0] - ev1.tau
    ku = np.unique(m.key.to_numpy())
    la, lo = grid.latlon(ku)
    P = lla_to_ecef(la, lo, dem(la, lo) + p.obs_h)
    tmin, tmax = m.t1.min() + q_lo - p.silence_pad_s - 60, m.t1.max() + q_hi + p.silence_pad_s + 60
    sil = []
    for trs, dmax in ((c["props"], p.silence_prop_m), (c["jets"], p.silence_jet_m)):
        padd = (dmax + 500) / M_PER_DEG
        for tr in trs:
            if tr.t[-1] < tmin or tr.t[0] > tmax:
                continue
            if tr.lat.max() < la.min() - padd or tr.lat.min() > la.max() + padd:
                continue
            r = cpa_points(tr, P, dmax, p.c)
            if len(r.t):
                sil.append(pd.DataFrame({"key": ku[r.pt], "flight": tr.id, "t": r.t}))
    if sil:
        s = pd.concat(sil, ignore_index=True)
        j = m[["key", "flight1", "flight2", "t1"]].reset_index().merge(s, on="key", suffixes=("", "_s"))
        j = j[(j.flight != j.flight1) & (j.flight != j.flight2) &
              (j.t > j.t1 + q_lo - p.silence_pad_s) & (j.t < j.t1 + q_hi + p.silence_pad_s)]
        m = m.drop(index=j["index"].unique()).reset_index(drop=True)
    cnt["silence"] = len(m)
    if m.empty:
        return pd.DataFrame(columns=COLS), cnt
    if len(m) > p.max_rows_tile:
        m = m.assign(dev=(m.t2 - m.t1 - c["off"]).abs()).nsmallest(p.max_rows_tile, "dev")
        cnt["truncated"] = 1

    sc = Scorer(c["all"], c["spec"], ev1, ev2, dem, p)
    q = grid.step / 4
    out = []
    for r in m.itertuples():
        la0, lo0 = grid.latlon(np.array([r.key]))
        for dn, de in ((-q, -q), (-q, q), (q, -q), (q, q)):
            la_, lo_ = offset_latlon(la0[0], lo0[0], dn, de)
            o = sc.point(r.flight1, r.flight2, float(la_), float(lo_))
            if o:
                out.append(o)
    cnt["scored"] = len(out)
    return pd.DataFrame(out, columns=COLS), cnt


def _tile_indep(tile, c, cnt, pad):
    """Cuts: cells that hear flight A at time 1 AND flight B at time 2, each time known on its own."""
    p, grid, dem, ev1, ev2 = c["p"], c["grid"], c["dem"], c["ev1"], c["ev2"]
    f1 = [t for t in c["c1"] if _overlaps(c["bb1"][t.id], tile, pad)]
    f2 = [t for t in c["c2"] if _overlaps(c["bb2"][t.id], tile, pad)]
    r1 = [x for x in (_flight_cells(t, ev1, c["tw1"], grid, tile, dem, p) for t in f1) if x is not None]
    r2 = [x for x in (_flight_cells(t, ev2, c["tw2"], grid, tile, dem, p) for t in f2) if x is not None]
    if not r1 or not r2:
        return pd.DataFrame(columns=COLS), cnt
    t1, t2 = pd.concat(r1, ignore_index=True), pd.concat(r2, ignore_index=True)
    cnt["cand1"], cnt["cand2"] = len(t1), len(t2)
    m = t1.merge(t2, on="key", suffixes=("1", "2"))
    m = m[m.flight1 != m.flight2].reset_index(drop=True)
    cnt["offset"] = cnt["silence"] = len(m)         # no offset/silence filters: the recording has cuts
    if m.empty:
        return pd.DataFrame(columns=COLS), cnt
    if len(m) > p.max_rows_tile:
        c1_, c2_ = sum(c["tw1"]) / 2, sum(c["tw2"]) / 2
        m = m.assign(dev=(m.t1 - c1_).abs() + (m.t2 - c2_).abs()).nsmallest(p.max_rows_tile, "dev")
        cnt["truncated"] = 1
    sc = Scorer(c["all"], c["spec"], ev1, ev2, dem, p)
    q, out = grid.step / 4, []
    for r in m.itertuples():
        la0, lo0 = grid.latlon(np.array([r.key]))
        for dn, de in ((-q, -q), (-q, q), (q, -q), (q, q)):
            la_, lo_ = offset_latlon(la0[0], lo0[0], dn, de)
            o = sc.point_indep(r.flight1, r.flight2, float(la_), float(lo_))
            if o:
                out.append(o)
    cnt["scored"] = len(out)
    return pd.DataFrame(out, columns=COLS), cnt


def _tile_single(tile, c, cnt, pad):
    p, grid, dem, ev1 = c["p"], c["grid"], c["dem"], c["ev1"]
    keep = []
    for tr in (t for t in c["c1"] if _overlaps(c["bb1"][t.id], tile, pad)):
        df = _flight_cells(tr, ev1, c["tw1"], grid, tile, dem, p)
        if df is None:
            continue
        cnt["cand1"] += len(df)
        df["pre"] = _prescore(tr, df, ev1, grid, dem, c["spec"], p)
        keep.append(df.nlargest(p.single_keep, "pre"))
    if not keep:
        return pd.DataFrame(columns=COLS), cnt
    m = pd.concat(keep, ignore_index=True).nlargest(p.single_keep, "pre")
    cnt["offset"] = cnt["silence"] = len(m)
    sc = Scorer(c["all"], c["spec"], ev1, None, dem, p)
    q, out = grid.step / 4, []
    for r in m.itertuples():
        la0, lo0 = grid.latlon(np.array([r.key]))
        for dn, de in ((-q, -q), (-q, q), (q, -q), (q, q)):
            la_, lo_ = offset_latlon(la0[0], lo0[0], dn, de)
            o = sc.point1(r.flight, float(la_), float(lo_))
            if o:
                out.append(o)
    cnt["scored"] = len(out)
    return pd.DataFrame(out, columns=COLS), cnt


def _spread(res, min_m, n):
    """Greedy: best-first, skipping rows within min_m of an already kept row from the same flight."""
    keep = []
    la, lo = res.lat.to_numpy(), res.lon.to_numpy()
    fl = res.flight1.to_numpy()
    for i in range(len(res)):
        if all(fl[j] != fl[i] or np.hypot((la[i] - la[j]) * M_PER_DEG,
               (lo[i] - lo[j]) * M_PER_DEG * np.cos(np.radians(la[i]))) >= min_m for j in keep):
            keep.append(i)
            if len(keep) >= n:
                break
    return res.iloc[keep]


def make_tiles(bbox, deg):
    s, w, n, e = bbox
    lats, lons = np.arange(s, n, deg), np.arange(w, e, deg)
    return [(a, b, min(a + deg, n), min(b + deg, e)) for a in lats for b in lons]


def crop_spec(spec, t_lo, t_hi):
    from .audio import Spec
    m = (spec.t >= t_lo) & (spec.t <= t_hi)
    return Spec(spec.S[:, m], spec.f, spec.t[m])


def locate(tracks, spec, ev1, ev2, bbox, dem=None, p=Params(), t0_window=None, progress=None, log=print, abs_times=None):
    """t0_window = (earliest, latest) epoch time the audio clip could have started; None = unknown.
    ev2=None -> one-curve mode (needs a known time: t0_window or abs_times).
    abs_times=[(T, tol), ...] = the real clock time (epoch s) AT each curve and its tolerance - for recordings with cuts.
    With two curves they are then searched independently and a location must fit both."""
    dem = dem or DEM()
    if ev2 is None and t0_window is None and abs_times is None:
        raise ValueError("one-curve search needs a known time")
    indep = abs_times is not None and ev2 is not None
    grid = Grid(bbox, p.grid_m)
    props = [t for t in tracks if not t.is_jet]
    jets = [t for t in tracks if t.is_jet]
    if abs_times is not None:
        win = [(T - tol - p.tau_slack_s, T + tol + p.tau_slack_s) for T, tol in abs_times]
        tw1, tw2 = win[0], (win[1] if ev2 is not None else None)
    elif t0_window is None:
        tw1 = tw2 = (-INF, INF)
    else:
        sl = (p.offset_tol_s + 5) if ev2 is not None else p.tau_slack_s       # one curve + exact clock: keep the window tight
        tw1 = (t0_window[0] + ev1.tau - sl, t0_window[1] + ev1.tau + sl)
        tw2 = None if ev2 is None else (t0_window[0] + ev2.tau - sl, t0_window[1] + ev2.tau + sl)
    bb1 = _narrow(props, ev1, tw1, p)
    bb2 = {} if ev2 is None else _narrow(props, ev2, tw2, p)
    c1 = [t for t in props if t.id in bb1]
    c2 = [t for t in props if t.id in bb2]
    info = dict(indep=indep, offset=None if ev2 is None else ev2.tau - ev1.tau, flights=len(tracks), props=len(props), cand_flights1=len(c1),
                cand_flights2=len(c2), counts=_empty_counts(), tiles=0)
    log(f"narrowing: {len(props)} non-jet flights -> {len(c1)} (event 1) / {len(c2)} (event 2) match speed+time")
    if not c1 or (ev2 is not None and not c2):
        return pd.DataFrame(columns=COLS + ["score"]), info
    tiles = [t for t in make_tiles(bbox, p.tile_deg)
             if any(_overlaps(b, t, 0.03) for b in bb1.values()) and (ev2 is None or any(_overlaps(b, t, 0.03) for b in bb2.values()))]
    info["tiles"] = len(tiles)
    log(f"{len(tiles)} tile(s) to search")
    ctx = dict(indep=indep, p=p, grid=grid, dem=dem, ev1=ev1, ev2=ev2, tw1=tw1, tw2=tw2, off=None if ev2 is None else ev2.tau - ev1.tau, c1=c1, c2=c2,
               bb1=bb1, bb2=bb2, props=props, jets=jets, all=tracks,
               spec=crop_spec(spec, ev1.win[0] - 20, (ev2 or ev1).win[1] + 20))
    results = []

    def done(res, k):
        results.append(res[0])
        for key, v in res[1].items():
            info["counts"][key] += v
        if progress:
            cn = info["counts"]
            progress(k / max(len(tiles), 1), f"tile {k}/{len(tiles)} | candidates {cn['cand1']:,}"
                     + ("" if ev2 is None else f"/{cn['cand2']:,} -> same-cell {cn['offset']:,} -> quiet {cn['silence']:,}")
                     + f" -> scored {cn['scored']:,}")

    if p.workers > 1 and len(tiles) > 1:
        with cf.ProcessPoolExecutor(p.workers, initializer=_init, initargs=(ctx,)) as ex:
            for k, fut in enumerate(cf.as_completed([ex.submit(_tile_task, t) for t in tiles]), 1):
                done(fut.result(), k)
    else:
        _init(ctx)
        for k, t in enumerate(tiles, 1):
            done(_tile_task(t), k)
    cn = info["counts"]
    log(f"candidates {cn['cand1']:,}/{cn['cand2']:,} -> offset {cn['offset']:,} -> silence {cn['silence']:,} -> scored {cn['scored']:,}")
    res = pd.concat([r for r in results if len(r)], ignore_index=True) if any(len(r) for r in results) else None
    if res is None:
        return pd.DataFrame(columns=COLS + ["score"]), info
    res["score"] = res.score1 + res.score2
    res = res.sort_values("score", ascending=False)
    allbest = res.loc[res.groupby(["flight1", "flight2"]).score.idxmax()]       # best point per flight (pair): the video's scatter
    info["pairs"] = allbest[["score1", "score2"]].round(2).head(3000).values.tolist()
    if ev2 is None:      # one curve + a time window fixes a stretch along the flight path, not a point: keep spread-out spots
        best = _spread(res, 1000.0, p.top_n)
    else:
        best = res.loc[res.groupby(["flight1", "flight2"]).score.idxmax()].sort_values("score", ascending=False).head(p.top_n)

    sc = Scorer(tracks, ctx["spec"], ev1, ev2, dem, p)         # fine refinement around the best few
    point = (sc.point_indep if indep else sc.point) if ev2 is not None else (lambda f1, f2, la, lo: sc.point1(f1, la, lo))
    steps = np.arange(-p.fine_radius_m, p.fine_radius_m + 1e-6, p.fine_step_m)
    best = best.reset_index(drop=True)
    for i in range(min(p.fine_top, len(best))):
        r, fine = best.iloc[i], []
        for dn in steps:
            for de in steps:
                la, lo = offset_latlon(r.lat, r.lon, dn, de)
                o = point(r.flight1, r.flight2, float(la), float(lo))
                if o:
                    fine.append(o)
        if fine:
            f = pd.DataFrame(fine, columns=COLS)
            f["score"] = f.score1 + f.score2
            if f.score.max() > r.score:
                best.loc[i, f.columns] = f.loc[f.score.idxmax()].to_numpy()
        if progress:
            progress(1.0, "refining")
    return best.sort_values("score", ascending=False).reset_index(drop=True), info


def heatmap(tracks, spec, ev1, ev2, dem, p, f1, f2, lat, lon, radius_m=1200.0, step_m=50.0, indep=False):
    """Fine-resolution score maps around one flight (pair), like the video's QGIS heat maps.
    Returns dict(e1, e2, both) 2-D arrays (north at the top), NaN where the flight is not near enough."""
    sc = Scorer(tracks, spec, ev1, ev2, dem, p)
    offs = np.arange(-radius_m, radius_m + 1e-6, step_m)
    n = len(offs)
    out = {k: np.full((n, n), np.nan) for k in ("e1", "e2", "both")}
    for i, dn in enumerate(offs[::-1]):
        for j, de in enumerate(offs):
            la, lo = offset_latlon(lat, lon, dn, de)
            o = ((sc.point_indep if indep else sc.point)(f1, f2, float(la), float(lo)) if ev2 is not None
                 else sc.point1(f1, float(la), float(lo)))
            if o:
                out["e1"][i, j] = o[4]
                if ev2 is not None:
                    out["e2"][i, j], out["both"][i, j] = o[5], o[4] + o[5]
    if ev2 is None:
        del out["e2"], out["both"]
    return out
