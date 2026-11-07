"""Physics: emission time, radial velocity, Doppler ratio, closest point of approach."""
from dataclasses import dataclass
import numpy as np

C_SOUND = 343.0  # m/s at ~20C; use 331.3 + 0.606*T for other temps


def interp_pos(tr, te, clip=True):
    te = np.asarray(te, float)
    if clip:
        te = np.clip(te, tr.t[0], tr.t[-1])
    return np.stack([np.interp(te, tr.t, tr.pos[:, k]) for k in range(3)], axis=-1)


def emission_time(tr, obs, th, c=C_SOUND, iters=5):
    """When must the plane have made the sound heard at time th? (fixed-point iteration)"""
    th = np.asarray(th, float)
    te = th - 2000.0 / c
    for _ in range(iters):
        r = np.linalg.norm(interp_pos(tr, te) - obs, axis=-1)
        te = th - r / c
    return te


def ratio_curve(tr, obs, th, c=C_SOUND):
    """c/(c+v_r) at the *emission* moment for each heard time th (NaN where track absent).
    Heard frequency = f_rest * ratio."""
    te = emission_time(tr, obs, th, c)
    valid = (te >= tr.t[0] + 1) & (te <= tr.t[-1] - 1)
    p = interp_pos(tr, te)
    v = interp_pos(tr, te + 0.5) - interp_pos(tr, te - 0.5)   # m/s (1 s baseline)
    los = p - obs
    los /= np.linalg.norm(los, axis=-1, keepdims=True)
    vr = np.sum(v * los, axis=-1)                              # + = receding
    out = c / (c + vr)
    out[~valid] = np.nan
    return out


@dataclass
class CPA:
    pt: np.ndarray   # index into the query point array
    t: np.ndarray    # heard time of CPA (emission time + sound travel)
    d: np.ndarray    # slant distance at CPA
    v: np.ndarray    # plane speed at CPA


def cpa_points(tr, P, d_hi, c=C_SOUND):
    """Closest approach of track to each ECEF point in P within d_hi (slant metres).
    d^2(t) is exactly quadratic for straight motion, so a 3-sample parabola gives the CPA."""
    n = len(tr.t)
    dist, idx = tr.tree.query(P, distance_upper_bound=d_hi * 1.3 + 200)
    ok = np.isfinite(dist) & (idx > 0) & (idx < n - 1)
    pt = np.where(ok)[0]
    i = idx[ok]
    Pk = P[ok]
    ym = np.sum((tr.pos[i - 1] - Pk) ** 2, axis=1)
    y0 = dist[ok] ** 2
    yp = np.sum((tr.pos[i + 1] - Pk) ** 2, axis=1)
    a = (ym + yp - 2 * y0) / 2
    b = (yp - ym) / 2
    good = a > 1e-9
    x = np.where(good, -b / (2 * np.where(good, a, 1)), 0.0)
    good &= np.abs(x) <= 1.5
    dmin = np.sqrt(np.maximum(y0 - b ** 2 / (4 * np.where(good, a, 1)), 0.0))
    dt = tr.t[1] - tr.t[0]
    t_e = tr.t[i] + x * dt
    speed = np.linalg.norm(tr.pos[i + 1] - tr.pos[i - 1], axis=1) / (tr.t[i + 1] - tr.t[i - 1])
    return CPA(pt[good], (t_e + dmin / c)[good], dmin[good], speed[good])


def ratio_straight(th_rel, v, d, c=C_SOUND, span=600.0, n=12001):
    """Doppler ratio for constant-velocity straight flight.
    th_rel = heard time relative to the heard CPA. Emission-time parameterisation (the
    'correct way' in the video), then mapped onto heard time."""
    u = np.linspace(-span, span, n)
    R = np.sqrt(d * d + (v * u) ** 2)
    t_rel = u + (R - d) / c
    vr = v * v * u / R
    return np.interp(th_rel, t_rel, c / (c + vr))


def ratio_many(tr, obs, th, c=C_SOUND, iters=4):
    """Vectorised ratio_curve: obs (n,3) observers, th (n,m) heard times -> (n,m) ratios (NaN where track absent)."""
    th = np.asarray(th, float)
    obs3 = obs[:, None, :]
    te = th - 2000.0 / c

    def pos(t):
        t = np.clip(t, tr.t[0], tr.t[-1])
        return np.stack([np.interp(t, tr.t, tr.pos[:, k]) for k in range(3)], axis=-1)

    for _ in range(iters):
        te = th - np.linalg.norm(pos(te) - obs3, axis=-1) / c
    p = pos(te)
    v = pos(te + 0.5) - pos(te - 0.5)
    los = p - obs3
    los /= np.linalg.norm(los, axis=-1, keepdims=True)
    out = c / (c + np.sum(v * los, axis=-1))
    out[(te < tr.t[0] + 1) | (te > tr.t[-1] - 1)] = np.nan
    return out
