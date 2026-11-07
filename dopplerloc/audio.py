"""Audio -> spectrogram -> Doppler measurements (the step done by hand in the video)."""
from dataclasses import dataclass
import subprocess, tempfile
import numpy as np
from math import gcd
from scipy import signal
from scipy.ndimage import uniform_filter1d
from scipy.optimize import least_squares
from .model import C_SOUND, ratio_straight


def load_audio(path, fs=2000):
    """Any audio/video file -> mono float32 at fs Hz (needs ffmpeg for non-wav)."""
    import scipy.io.wavfile as wf
    if not path.lower().endswith(".wav"):
        tmp = tempfile.mktemp(suffix=".wav")
        subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", str(fs), tmp],
                       check=True, capture_output=True)
        path = tmp
    sr, x = wf.read(path)
    x = x.astype(np.float32)
    if x.ndim > 1:
        x = x.mean(1)
    if sr != fs:
        g = gcd(int(sr), int(fs))
        x = signal.resample_poly(x, fs // g, sr // g)
    return x / (np.abs(x).max() + 1e-12), fs


@dataclass
class Spec:
    S: np.ndarray   # (nf, nt) dB above each frequency row's median (background ~ 0)
    f: np.ndarray
    t: np.ndarray

    def sample(self, F, T):
        """Nearest-bin lookup; anything off the grid / NaN counts as background (0)."""
        df, dt = self.f[1] - self.f[0], self.t[1] - self.t[0]
        F, T = np.broadcast_arrays(np.nan_to_num(F, nan=-1e9), T)
        fi = np.rint((F - self.f[0]) / df).astype(np.int64)
        ti = np.rint((T - self.t[0]) / dt).astype(np.int64)
        ok = (fi >= 0) & (fi < len(self.f)) & (ti >= 0) & (ti < len(self.t))
        out = np.zeros(F.shape)
        out[ok] = self.S[fi[ok], ti[ok]]
        return out


def make_spec(x, fs, nperseg=4096, hop=500, fmax=900, blur=(1.0, 1.5)):
    f, t, P = signal.spectrogram(x, fs, window="hann", nperseg=nperseg,
                                 noverlap=nperseg - hop, mode="psd")
    keep = f <= fmax
    S = 10 * np.log10(P[keep] + 1e-20)
    S -= np.median(S, axis=1, keepdims=True)
    if blur:                       # tolerate small timing/pitch error in the scoring (freq bins, time bins)
        from scipy.ndimage import gaussian_filter
        S = gaussian_filter(S, blur)
    return Spec(S, f[keep], t)


def detect_events(spec, band=(60, 500), min_len=25.0, z=4.0, merge_gap=45.0, pad=30.0):
    """Crude tonal-event finder: frames whose strongest in-band bin is unusually high.
    Returns [(t_start, t_end, strength)] strongest first. Manual windows beat this on hard audio."""
    m = (spec.f >= band[0]) & (spec.f <= band[1])
    dt = spec.t[1] - spec.t[0]
    peak = uniform_filter1d(spec.S[m].max(0), max(1, int(4 / dt)))
    med = np.median(peak)
    mad = 1.4826 * np.median(np.abs(peak - med)) + 1e-9
    hot = (peak - med) / mad > z
    runs, i = [], 0
    while i < len(hot):
        if hot[i]:
            j = i
            while j + 1 < len(hot) and hot[j + 1]:
                j += 1
            runs.append([spec.t[i], spec.t[j], float(peak[i:j + 1].sum())])
            i = j + 1
        else:
            i += 1
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < merge_gap:
            merged[-1][1] = r[1]; merged[-1][2] += r[2]
        else:
            merged.append(r)
    out = [(a - pad, b + pad, s) for a, b, s in merged if b - a >= min_len]
    return sorted(out, key=lambda e: -e[2])


def track_ridge(spec, band, win, lam=1.5, max_jump=4):
    """Viterbi ridge: brightest smooth path through the spectrogram inside band/win."""
    fi = np.where((spec.f >= band[0]) & (spec.f <= band[1]))[0]
    ti = np.where((spec.t >= win[0]) & (spec.t <= win[1]))[0]
    sub = spec.S[np.ix_(fi, ti)]
    nf, nt = sub.shape
    score = np.full((nf, nt), -np.inf)
    back = np.zeros((nf, nt), int)
    score[:, 0] = sub[:, 0]
    idx = np.arange(nf)
    for k in range(1, nt):
        best = np.full(nf, -np.inf)
        arg = np.zeros(nf, int)
        for dj in range(-max_jump, max_jump + 1):
            sh = np.roll(score[:, k - 1], dj)
            if dj > 0: sh[:dj] = -np.inf
            elif dj < 0: sh[dj:] = -np.inf
            cand = sh - lam * abs(dj)
            better = cand > best
            best[better] = cand[better]
            arg[better] = (idx - dj)[better]
        score[:, k] = best + sub[:, k]
        back[:, k] = arg
    path = np.zeros(nt, int)
    path[-1] = np.argmax(score[:, -1])
    for k in range(nt - 1, 0, -1):
        path[k - 1] = back[path[k], k]
    return spec.t[ti], spec.f[fi][path], sub[path, np.arange(nt)]


@dataclass
class Event:
    tau: float      # audio time of heard CPA (s)
    f_rest: float   # rest frequency of the fitted ridge (Hz)
    v: float        # speed (m/s)
    d: float        # CPA slant distance (m)
    win: tuple      # audio window (s)
    rms: float      # fit residual (Hz)
    n: int          # points used
    strength: float = 0.0   # median ridge brightness above background (dB)
    manual: bool = False
    trace: tuple = None     # hand-traced (t, f) points; used to score faint curves by closeness to the line

    def quality(self, min_db=6.0, min_n=20, max_rms=6.0):
        """None if the automatic measurement looks trustworthy, else a reason to ask the user."""
        if self.n < min_n:
            return f"only {self.n} ridge points found"
        if self.strength < min_db:
            return f"curve is faint ({self.strength:.1f} dB above background)"
        if self.rms > max_rms:
            return f"curve fits poorly ({self.rms:.1f} Hz error)"
        return None


def _fit_model(t, f, w, win, c, manual=False, strength=0.0):
    fs_ = uniform_filter1d(f, min(5, len(f)))
    slope = np.gradient(fs_, t)
    k = int(np.argmin(slope))
    tau0, f0 = t[k], fs_[k]
    v0 = float(np.clip(c * (np.percentile(f, 95) - np.percentile(f, 5)) / (2 * f0), 30, 200))
    lo = [0.7 * f0, 15.0, 30.0, win[0]]
    hi = [1.3 * f0, 260.0, 12000.0, win[1]]
    wt = np.sqrt(np.clip(w, 1, 30) / 10)

    def resid(p):
        f0_, v_, d_, tc = p
        return (f0_ * ratio_straight(t - tc, v_, d_, c) - f) * wt

    best = None
    for d0 in (100, 300, 1000, 3000):
        r = least_squares(resid, [f0, v0, d0, tau0], bounds=(lo, hi), loss="soft_l1", f_scale=1.0)
        if best is None or r.cost < best.cost:
            best = r
    p = best.x
    rms = float(np.sqrt(np.mean((resid(p) / wt) ** 2)))
    return Event(tau=float(p[3]), f_rest=float(p[0]), v=float(p[1]), d=float(p[2]), win=tuple(win), rms=rms,
                 n=len(t), strength=float(strength), manual=manual)


def fit_event(spec, band, win, c=C_SOUND, min_db=3.0):
    """Automatic: track the ridge, fit the straight-line Doppler model (f_rest, v, d, tau)."""
    t, f, w = track_ridge(spec, band, win)
    keep = w > min_db
    if keep.sum() < 8:
        raise RuntimeError(f"no clear ridge in window {win}, band {band}")
    return _fit_model(t[keep], f[keep], w[keep], win, c, strength=float(np.median(w[keep])))


def fit_points(points, win, c=C_SOUND):
    """Manual: the user clicked points (audio seconds, Hz) along the curve."""
    pts = np.array(sorted(points), float)
    if len(pts) < 5:
        raise RuntimeError("click at least 5 points along the curve")
    ev = _fit_model(pts[:, 0], pts[:, 1], np.full(len(pts), 20.0), win, c, manual=True, strength=99.0)
    ev.trace = tuple((float(t), float(f)) for t, f in pts)
    return ev


def make_spec_view(x, fs, t0, t1, nperseg, fmax=900):
    """Higher/lower-resolution spectrogram of just [t0,t1] for the manual views ('zoom and increase the resolution')."""
    a, b = max(0, int((t0 - 4) * fs)), min(len(x), int((t1 + 4) * fs))
    sp = make_spec(x[a:b], fs, nperseg=min(nperseg, max(256, b - a - 1)), hop=max(100, nperseg // 8), fmax=fmax)
    return Spec(sp.S, sp.f, sp.t + a / fs)
