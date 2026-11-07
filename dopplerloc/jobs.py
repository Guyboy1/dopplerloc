"""The whole pipeline as a Job with named steps, progress, errors, and manual hand-offs."""
import os, threading, time, traceback, uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from . import media, providers, typedb, images
from .audio import load_audio, make_spec, detect_events, fit_event, fit_points
from .geo import DEM, TerrainTilesDEM, lla_to_ecef
from .search import heatmap
from .model import ratio_curve
from .search import Params, locate
from .timeframe import Timeframe, describe, local_str, parse_local
from .tracks import build_tracks

STEPS = ["Reading the audio", "Checking location and time", "Finding the aircraft sounds",
         "Measuring the Doppler curves", "Getting flight data", "Searching", "Checking the results"]


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, asker=None):
        self.id = uuid.uuid4().hex[:10]
        self.steps = [dict(name=n, state="waiting", msg="") for n in STEPS]
        self.cur, self.frac = 0, 0.0
        self.status, self.error, self.result = "running", None, None
        self.logs, self.manual, self.seq = [], None, 0
        self.spec, self.evs, self.preview = None, None, []
        self.audio, self.heat = None, {}
        self.overlays = {}
        self.asker = asker                  # tests / scripts can answer manual requests programmatically
        self._evt, self._resp, self.cancelled = threading.Event(), None, False
        # timing: elapsed time without the waits for the user, the download counters, and what is still to do
        self.t_start, self.paused, self.step_t0 = time.time(), 0.0, time.time()
        self.xfer, self.source = providers.Transfer(), None
        self.plan_n, self.wi, self.search_durs, self._search_t0 = None, 0, [], None
        self._pct_max, self._search_guess = 0, True

    # ---- bookkeeping
    def log(self, msg):
        self.logs.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def step(self, i, state="running", msg=""):
        if i != self.cur or state == "running":
            self.step_t0 = time.time()
        self.cur = i
        self.steps[i].update(state=state, msg=msg or self.steps[i]["msg"])
        if state == "running":
            self.frac = 0.0

    def progress(self, frac, msg=""):
        self.frac = frac
        if msg:
            self.steps[self.cur]["msg"] = msg

    # ---- time estimate. Remaining time = (bytes left / measured download speed, overlapped with scanning) + search time left.
    EARLY = (4.0, 1.0, 5.0, 4.0)                # seconds for steps 1-4 (audio, checks, finding events, measuring curves)
    DEFAULT_DL, DEFAULT_SCAN = 8e6, 20e6         # bytes/s guesses, used only until the real speed has been measured
    SEARCH_GUESS, REVIEW_GUESS, UNIT_GUESS = 40.0, 6.0, 60.0

    def active_elapsed(self):
        return max(0.0, time.time() - self.t_start - self.paused)

    def eta(self):
        """(seconds remaining or None, rough?)  'rough' = still using default speeds because nothing has been measured yet."""
        if self.status != "running" or self.plan_n is None:
            return None, False
        now, x, rough, rem = time.time(), self.xfer, False, 0.0
        if self.cur < 4:
            rem += max(1.0, self.EARLY[self.cur] - (now - self.step_t0)) + sum(self.EARLY[self.cur + 1:])
        in_data = self.cur <= 4
        if in_data and self.source == "adsblol":
            dl, sc = x.dl_speed, x.scan_speed
            if not dl:
                dl, rough = self.DEFAULT_DL, True
            if not sc:
                sc, rough = self.DEFAULT_SCAN, True
            rem += max(x.dl_bytes_left() / dl, x.scan_bytes_left() / sc)
        elif in_data and self.source == "opensky":
            if not x.unit_secs:
                rough = True
            rem += x.units_left * (x.unit_secs or self.UNIT_GUESS)
        # searching: windows not started yet + the rest of the current one
        avg = (sum(self.search_durs) / len(self.search_durs)) if self.search_durs else None
        left = self.plan_n - self.wi - (1 if self.cur >= 5 else 0)
        if self.cur == 5 and self._search_t0:
            el = now - self._search_t0
            cur_rem = el / self.frac * (1 - self.frac) if self.frac > 0.05 and el > 3 else max(0.0, (avg or self.SEARCH_GUESS) - el)
            rem += cur_rem
            if not avg and self.frac <= 0.05:
                self._search_guess = True
        elif self.cur < 5:
            left += 1 if self.cur == 4 else 0
        self._search_guess = avg is None and not (self.cur == 5 and self.frac > 0.05)
        rem += max(0, left) * (avg or self.SEARCH_GUESS) + self.REVIEW_GUESS
        return rem, rough

    @property
    def percent(self):
        if self.status == "done":
            return 100
        eta, _ = self.eta()
        if eta is None:
            pct = int(3 * (self.cur + self.frac))
        else:
            el = self.active_elapsed()
            pct = int(100 * el / max(el + eta, 1.0))
        self._pct_max = max(self._pct_max, min(pct, 99))
        return self._pct_max

    def timing(self):
        eta, rough = self.eta()
        x, dl = self.xfer, None
        if self.source == "adsblol" and self.cur <= 4 and self.status == "running":
            left = x.dl_bytes_left()
            if left > 0 or x.dl_speed:
                dl = dict(done=x.dl_done, total=x.dl_done + left, speed=x.dl_speed, scan_speed=x.scan_speed)
        return dict(eta_s=None if eta is None else round(eta), eta_rough=rough, eta_search_guess=self._search_guess, elapsed_s=round(self.active_elapsed()),
                    waiting=self.manual is not None, dl=dl)

    # ---- manual hand-off
    def ask(self, kind, payload):
        if self.asker:
            r = self.asker(kind, payload, self)
        else:
            self.seq += 1
            self.manual = dict(kind=kind, seq=self.seq, **payload)
            self.status = "waiting"
            self._evt.clear()
            t_wait = time.time()
            self._evt.wait()
            self.paused += time.time() - t_wait               # the time you spend choosing does not count towards the estimate
            r, self.manual, self.status = self._resp, None, "running"
        if self.cancelled or (r or {}).get("cancel"):
            raise Cancelled()
        return r

    def answer(self, resp):
        self._resp = resp
        self._evt.set()

    def cancel(self):
        self.cancelled = True
        self._evt.set()


def _make_dem(opt, bbox, job):
    if opt in (None, "", "flat"):
        return DEM()
    if opt == "auto":
        return TerrainTilesDEM(log=job.log)
    return DEM.from_geotiff(opt, bbox)


def _local(epoch, tz):
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z")


def run(job, o):
    """o: dict of options (see web.py / README). Returns nothing; fills job.result."""
    try:
        _run(job, o)
        job.status = "done"
        job.steps[job.cur]["state"] = "done"
    except Cancelled:
        job.status, job.error = "error", "Stopped."
    except Exception as e:
        job.status, job.error = "error", f"{type(e).__name__}: {e}"
        job.steps[job.cur]["state"] = "error"
        job.log(traceback.format_exc())


def _run(job, o):
    if not o.get("ack"):
        raise RuntimeError("You must confirm the permission/responsible-use statement first.")
    p = Params(**{k: v for k, v in (o.get("params") or {}).items() if k in Params.__dataclass_fields__})
    if o.get("temp_c") is not None:
        p.c = 331.3 + 0.606 * float(o["temp_c"])
    p.workers = int(o.get("workers") or p.workers)

    # 1 -- audio
    job.step(0)
    wav, info = media.extract_audio(o["audio_path"]) if not o["audio_path"].lower().endswith(".wav") or o.get("force_ffmpeg") else (o["audio_path"], {"duration": 0})
    x, fs = load_audio(wav)
    dur = len(x) / fs
    job.log(f"audio: {dur:.0f} s")
    if dur < 60:
        raise RuntimeError(f"The audio is only {dur:.0f} s long. At least a minute or two is needed "
                           "to catch aircraft passing (the method needs two events or one with a known exact time).")
    job.audio = (x, fs)
    job.spec = spec = make_spec(x, fs)
    job.step(0, "done", f"{dur / 60:.1f} min of audio")

    # 2 -- location & time
    job.step(1)
    bbox = tuple(float(v) for v in o["bbox"])
    if not (-90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180):
        raise RuntimeError("search box must be south < north and west < east, in degrees")
    try:
        tf = Timeframe(**o["timeframe"])
        windows = tf.windows()
    except ValueError as e:
        raise RuntimeError(str(e))
    if o.get("metadata_end") and tf.is_exact:       # the file's time marks the END of the clip: the start is `duration` earlier
        _shift_start(tf, dur)
        windows = tf.windows()
        job.log(f"metadata time treated as the END of the clip -> start {local_str(windows[0][0], tf.tz)}")
    cuts, exact = tf.cuts, tf.is_exact
    area_km2 = (bbox[2] - bbox[0]) * 111 * (bbox[3] - bbox[1]) * 111 * np.cos(np.radians((bbox[0] + bbox[2]) / 2))
    est = providers.estimate(o["source"], windows)
    low = bool(o.get("low_storage")) and o["source"] == "adsblol"
    job.source = o["source"]
    from .coverage import assess
    cov = assess(o.get("country"), bbox)
    if cov["level"] != "none":
        job.log(f"NOTE: {cov['label']}{' - ' + cov['area'] if cov['area'] else ''}: {cov['reason']} A miss here may mean missing flight data, not a bad recording.")
    job.log(f"search box ~{area_km2:,.0f} km2; {describe(windows)}; recording type: {'WITH CUTS' if cuts else 'continuous'}")
    if o["source"] == "adsblol":
        free = providers.free_gb()
        need = 4.5 if low else est["gb"] + 1.0
        job.log(f"disk space: {free:.1f} GB free; this run needs about {need:.1f} GB ({'low-storage mode: one day at a time' if low else 'all days are kept'})")
        if low and free < need:
            raise providers.ProviderError(f"Not enough free disk space: even low-storage mode needs about {need:.1f} GB free and only {free:.1f} GB is available. "
                                          "Free some space (the page can delete old downloaded flight data) and try again.")
        if not low and free < need:
            job.ask("confirm", dict(message=f"Only {free:.1f} GB of disk space is free, but downloading {est['note'].split('about ')[-1].split(' to')[0]} "
                                            f"and keeping it needs about {need:.1f} GB. Tick 'Low storage mode' (Advanced) to use only about 4.5 GB at a time. "
                                            "Continue anyway?"))
    if est["gb"] > 0.5 or len(windows) > 3:
        job.ask("confirm", dict(message=f"This search covers {describe(windows)} over ~{area_km2:,.0f} km2. {est['note']} "
                                       f"{'Low-storage mode: one day at a time, deleted after use. ' if low else ''}It may take a long time. Continue?"))
    # first guess for the time estimate (refined once the real plan exists after step 4)
    if cuts:
        _expect(job, o, [(min(a for a, _ in windows), max(b for _, b in windows))], 1)
    else:
        _expect(job, o, [(tf.t0_window(w)[0] - 30, tf.t0_window(w)[1] + dur + 60) for w in windows], len(windows))
    job.step(1, "done", f"{describe(windows)}; box ~{area_km2:,.0f} km2")

    # 3 -- events.  Curves needed: continuous+exact start -> 1; continuous+no exact start -> 2; cuts -> 1 (each gets its own time)
    need = 1 if (cuts or exact) else 2
    job.log({(False, True): "Exact start time: one clear curve is enough (two is better).",
             (False, False): "Start time is not exact: the search only continues with at least TWO clear Doppler curves "
                             "(or give the exact start time).",
             (True, True): "Recording with cuts: each curve will be pinned to its own real clock time."}.get((cuts, exact), ""))
    job.step(2)
    band = tuple(o.get("band") or (60, 500))
    cands = sorted(detect_events(spec, band)[:8])          # everything the detector liked, in time order (shown one by one in manual mode)
    found = sorted(sorted(cands, key=lambda c: -c[2])[:2])
    job.preview = [dict(win=(a, b), band=band, color=(255, 255, 0)) for a, b, _ in found]
    events, why = [dict(win=(a, b), band=band) for a, b, _ in found], None
    if o.get("always_manual"):
        why = "You chose to select the aircraft yourself."
    elif len(events) < need:
        why = (f"Automatic detection found only {len(events)} clear aircraft sound(s) and this setup needs {need} (the recording may be noisy)."
               if len(events) else "Automatic detection did not find a clear aircraft sound (the recording may be noisy).")
    while why is not None:
        r = job.ask("events", dict(reason=why, need=need, category=("cuts" if cuts else "exact" if exact else "two"),
                                   allow_exact=(not cuts and not exact), duration=dur,
                                   fmax=float(spec.f[-1]), suggestions=[dict(win=[a, b]) for a, b, _ in found], band=list(band),
                                   candidates=[dict(win=[float(a), float(b)], strong=any(abs(a - fa) < 1 for fa, _, _ in found))
                                               for a, b, s_ in cands]))
        events = [e for e in r.get("events", []) if e.get("win") and e.get("band") and e["win"][1] > e["win"][0]][:2]
        if len(events) == 1 and r.get("exact") and not cuts and not exact:       # one curve + the exact start time
            try:
                tf.exact, tf.start, tf.end, tf.span_days = r["exact"], None, None, 1
                tf.tolerance_s = float(r.get("tolerance") or tf.tolerance_s)
                windows = tf.windows()
            except ValueError as e:
                events, why = [], f"{e}"
                tf.exact = None
                continue
            exact, need = True, 1
            job.log(f"exact start time {tf.exact} (+-{tf.effective_tolerance:g} s) given: one curve will be used")
        if len(events) >= need:
            why = None
        elif need == 2:
            why = (f"You have marked {len(events)} of the 2 curves needed. Mark another aircraft, or type the exact start time "
                   "(to the second) so that one curve is enough.")
        else:
            why = "Mark at least one curve."
    job.preview = [dict(win=tuple(e["win"]), band=tuple(e["band"]), color=(255, 255, 0)) for e in events]
    job.step(2, "done", f"{len(events)} event(s): {', '.join(f'{e['win'][0]:.0f}-{e['win'][1]:.0f}s' for e in events)}")

    # 4 -- measure each event (auto, falling back to hand-tracing)
    job.step(3)
    evs = []
    for i, e in enumerate(events[:2]):
        win, bnd, ev = tuple(e["win"]), tuple(e["band"]), None
        reason, skipped = None, False
        force_manual = bool(o.get("always_manual_trace"))
        while ev is None and not skipped:
            if not force_manual and reason is None:
                try:
                    ev = fit_event(spec, bnd, win, p.c)
                    reason = ev.quality()
                    if reason is None:
                        break
                    ev = None
                except RuntimeError as ex:
                    reason = str(ex)
            can_skip = (len(events) - 1) >= need           # only if the remaining curves still satisfy the rule
            r = job.ask("trace", dict(index=i + 1, reason=reason or "you asked to trace by hand", auto=bool(reason), win=list(win),
                                      band=list(bnd), can_skip=can_skip))
            if r.get("skip") and can_skip:
                skipped = True
            elif r.get("points"):
                try:
                    ev = fit_points([tuple(pt) for pt in r["points"]], win, p.c)
                except RuntimeError as ex:               # too few points etc. -> ask again
                    reason, force_manual = str(ex), True
            else:                                        # retry automatic fit with a new band / window
                win = tuple(r.get("win") or win); bnd = tuple(r.get("band") or bnd); reason, force_manual = None, False
        if skipped:
            job.log(f"event {i + 1} skipped")
            continue
        job.log(f"event {i + 1}: tau={ev.tau:.1f}s f={ev.f_rest:.1f}Hz v={ev.v:.0f}m/s d={ev.d:.0f}m"
                f"{' (hand-traced)' if ev.manual else ''}")
        evs.append(ev)
        job.preview = [dict(win=x.win, band=(max(x.f_rest * 0.6, 20), x.f_rest * 1.4), color=(0, 255, 120)) for x in evs] + \
                      [dict(win=tuple(q["win"]), band=tuple(q["band"]), color=(255, 255, 0)) for q in events[i + 1:]]
    if len(evs) < need:
        raise RuntimeError(f"Only {len(evs)} Doppler curve(s) could be measured; this setup needs {need}.")
    job.evs = evs

    # recording with cuts: now that the curves are known, ask for the real clock time at each one
    abs_times = None
    if cuts:
        start0 = windows[0][0]
        date0 = tuple(int(x) for x in local_str(start0, tf.tz)[:10].split("-"))
        reason = None
        while abs_times is None:
            r = job.ask("curve_times", dict(
                reason=reason, tz=tf.tz, tolerance=tf.tolerance_s,
                curves=[dict(index=i + 1, tau=float(ev.tau), suggested=local_str(start0 + ev.tau, tf.tz)[11:]) for i, ev in enumerate(evs)]))
            try:
                ts, tols = r["times"], r.get("tolerances") or [tf.tolerance_s] * len(evs)
                abs_times = [(parse_local(str(ts[i]), tf.tz, date0), max(float(tols[i]), 0.0)) for i in range(len(evs))]
            except (ValueError, KeyError, IndexError) as ex:
                reason, abs_times = f"could not read those times ({ex}); use HH:MM:SS", None
        job.log("curve clock times: " + ", ".join(f"{local_str(t, tf.tz)} (+-{tol:g}s)" for t, tol in abs_times))
    job.step(3, "done", "speeds " + " / ".join(f"{e.v:.0f}" for e in evs) + " m/s, distances " + " / ".join(f"{e.d:.0f}" for e in evs) + " m")

    # 5/6 -- flight data + search, per time window; 7 -- review (with optional wider retries)
    dem = _make_dem(o.get("dem"), bbox, job)
    D = dur
    # (fetch windows, possible-start window, per-curve clock times). Cuts: one combined search around the curve times.
    if cuts:
        plan = [dict(fetch=[(T - tol - p.tau_slack_s - 150, T + tol + p.tau_slack_s + 150) for T, tol in abs_times],
                     t0=None, abs=abs_times)]
    else:
        plan = [dict(fetch=[(tf.t0_window(w)[0] - 30, tf.t0_window(w)[1] + D + 60)], t0=tf.t0_window(w), abs=None) for w in windows]
    _expect(job, o, [f for w in plan for f in w["fetch"]], len(plan))
    x = job.xfer
    memo = {}
    attempts = 0
    while True:
        all_res, per_window, pairs = [], [], []
        for wi, w in enumerate(plan):
            job.wi = wi
            job.step(4, "running", f"window {wi + 1}/{len(plan)}")
            fbox = (max(bbox[0] - 0.2, -90), max(bbox[1] - 0.2, -180), min(bbox[2] + 0.2, 90), min(bbox[3] + 0.2, 180))
            frames = []
            with _opensky_login(o):
                for (t_lo, t_hi) in w["fetch"]:
                    # a margin beyond the search box lets flights near its edge keep their whole path
                    frames.append(providers.fetch(o["source"], fbox, t_lo, t_hi, o.get("flight_file"), job.log,
                                                  lambda g, t: job.progress(g / t if t else 0, f"downloaded {g / 1e6:.0f} MB"),
                                                  low_storage=low, x=x, memo=memo))
            df = pd.concat([f for f in frames if f is not None and len(f)], ignore_index=True) if any(f is not None and len(f) for f in frames) else None
            if df is None or len(df) == 0:
                job.log(f"no flight data for window {wi + 1}")
                continue
            df = df.drop_duplicates(["icao24", "time"])
            df = typedb.flag_jets(df, job.log)
            tracks = build_tracks(df)
            job.log(f"{len(tracks)} flight tracks ({sum(t.is_jet for t in tracks)} known jets)")
            job.step(4, "done", f"{len(tracks)} flight tracks")
            job.step(5, "running", f"window {wi + 1}/{len(plan)}")
            job._search_t0 = time.time()
            res, inf = locate(tracks, spec, evs[0], evs[1] if len(evs) > 1 else None, bbox, dem, p, w["t0"],
                              lambda f, m: job.progress(f, m), job.log, abs_times=w["abs"])
            job.search_durs.append(time.time() - job._search_t0)
            pairs += inf.get("pairs", [])
            if len(res):
                res["window"] = wi
                all_res.append(res)
                per_window.append((tracks, wi))
                if low and o.get("stop_early", True) and not o.get("always_manual") and wi < len(plan) - 1 and len(evs) > 1:
                    cur = pd.concat(all_res, ignore_index=True).sort_values("score", ascending=False)
                    mg = float(cur.score.iloc[0] - cur.score.iloc[1]) if len(cur) > 1 else None
                    if (mg is None or mg >= 3.0) and cur.score.iloc[0] >= 12:
                        job.log(f"clear winner already found in window {wi + 1}/{len(plan)}; skipping the remaining days "
                                "(low-storage mode stops early; untick 'stop as soon as a clear winner is found' to search all days)")
                        break
        job.step(5, "done")
        job.step(6)
        if not all_res:
            msg = ("No location matched. Possible reasons: no ADS-B coverage there, the time/place guess is wrong, "
                   "the clock time is off by more than the tolerance, or the speed/distance tolerances are too tight.")
            r = job.ask("review", dict(rows=[], message=msg, can_retry=attempts < 3, bbox=list(bbox), mode="two", tracks={}))
        else:
            res = pd.concat(all_res, ignore_index=True).sort_values("score", ascending=False).reset_index(drop=True)
            margin = float(res.score.iloc[0] - res.score.iloc[1]) if len(res) > 1 else None
            indep = bool(cuts and len(evs) > 1)
            rows, tracks_out = _rows(job, res, per_window, evs, p, dem, tf.tz, indep)
            job.result = dict(rows=rows, margin=margin, windows=len(plan), bbox=list(bbox), mode="two" if len(evs) > 1 else "one",
                              indep=indep, tracks=tracks_out, pairs=pairs, heat=[])
            _heat(job, res.iloc[0], per_window, evs, p, dem, indep)
            confident = len(evs) > 1 and (margin is None or margin >= 3.0) and res.score.iloc[0] >= 12
            if confident and not o.get("always_manual"):
                job.step(6, "done", "clear winner")
                return
            r = job.ask("review", dict(rows=rows, margin=margin, can_retry=attempts < 3,
                                       bbox=list(bbox), mode=job.result["mode"], tracks=tracks_out,
                                       message=("Only one Doppler curve was used. That identifies the aircraft but not which side of its path you "
                                                "were on, so candidates appear on BOTH sides of the flight path (mirror images), spread along it "
                                                "by your clock tolerance. A second curve (or a tighter clock) resolves this."
                                                if len(evs) == 1 else
                                                "The best match is not clearly better than the others. Look at the overlays and decide.")))
        if not r.get("retry"):
            return
        attempts += 1
        for k, v in r["retry"].items():
            if k in Params.__dataclass_fields__:
                setattr(p, k, float(v))
        job.log(f"retrying with {r['retry']}")


def _expect(job, o, ranges, n_windows):
    """Tell the job how much data and how many searches are ahead, so it can estimate the remaining time."""
    x = job.xfer
    job.plan_n = n_windows
    if o["source"] == "adsblol":
        ds = providers.utc_days(ranges)
        x.scan_days_left = len(ds)
        x.dl_days_left = sum(1 for d in ds if not providers.cached_day(d))
    elif o["source"] == "opensky":
        x.units_left = sum(int((hi - lo) // 3600) + 1 for lo, hi in ranges)


class _opensky_login:
    """Hand the OpenSky username/password typed in the page to pyopensky for this fetch only (never written to disk)."""
    def __init__(self, o):
        self.u, self.pw = (o.get("opensky_user") or "").strip().lower(), o.get("opensky_pass") or ""   # OpenSky stores usernames in lowercase
        self.active = o.get("source") == "opensky" and bool(self.u and self.pw)

    def __enter__(self):
        if self.active:
            self.old = (os.environ.get("OPENSKY_USERNAME"), os.environ.get("OPENSKY_PASSWORD"))
            os.environ["OPENSKY_USERNAME"], os.environ["OPENSKY_PASSWORD"] = self.u, self.pw

    def __exit__(self, *a):
        if self.active:
            for k, v in zip(("OPENSKY_USERNAME", "OPENSKY_PASSWORD"), self.old):
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _shift_start(tf, seconds):
    t = tf.windows()[0][0] - seconds
    d = datetime.fromtimestamp(t, ZoneInfo(tf.tz))
    tf.year, tf.month, tf.day, tf.exact = d.year, d.month, d.day, d.strftime("%H:%M:%S")


def _heat(job, top, per_window, evs, p, dem, indep=False):
    """Fine heat maps for the winning flight (pair): event 1, event 2, combined (as in the video's QGIS view)."""
    try:
        job.step(6, "running", "making heat maps")
        tracks = next(t for t, wi in per_window if wi == top.window)
        g = heatmap(tracks, job.spec, evs[0], evs[1] if len(evs) > 1 else None, dem, p, top.flight1, top.flight2 or None,
                    float(top.lat), float(top.lon), indep=indep)
        names = {"e1": "event 1", "e2": "event 2", "both": "combined"}
        job.heat = {k: images.render_heat(v, label=names[k]) for k, v in g.items()}
        job.result["heat"] = list(job.heat)
    except Exception as e:
        job.log(f"heat maps skipped: {e}")


def _rows(job, res, per_window, evs, p, dem, tz, indep=False):
    """Top candidates (nearby duplicates merged), overlay curves for the UI, and the flight path for one-curve mode."""
    by_w = {wi: {t.id: t for t in tr} for tr, wi in per_window}
    two = len(evs) > 1
    kept = []
    for r in res.itertuples():                                   # merge candidates closer than 300 m (two-curve mode)
        if two and any(np.hypot((r.lat - q.lat) * 111320, (r.lon - q.lon) * 111320 * np.cos(np.radians(r.lat))) < 300 for q in kept):
            continue
        kept.append(r)
        if len(kept) >= 10:
            break
    top = kept[0].score
    rows, paths = [], {}
    for k, r in enumerate(kept):
        tracks = by_w[r.window]
        obs = lla_to_ecef(r.lat, r.lon, float(dem(np.array([r.lat]), np.array([r.lon]))[0]) + p.obs_h)
        specs = [(evs[0], r.flight1, r.t1c, r.f1rest, evs[0].tau + r.delta)]
        if two:
            specs.append((evs[1], r.flight2, r.t2c, r.f2rest,
                          evs[1].tau + r.delta2 if indep else evs[0].tau + r.delta + (r.t2c - r.t1c)))
        curves = []
        for ev, fid, tc, frest, tau_place in specs:
            rel = np.arange(ev.win[0] - ev.tau, ev.win[1] - ev.tau, 0.5)
            ra = ratio_curve(tracks[fid], obs, tc + rel, p.c)
            curves.append(dict(win=ev.win, t=(tau_place + rel).tolist(), f=[(m * frest * ra).tolist() for m in p.mults]))
        job.overlays[k] = curves
        t0 = r.t1c - (evs[0].tau + r.delta)
        if not two and r.flight1 not in paths:                   # aircraft path near the candidates, for the map
            tr = tracks[r.flight1]
            m = (tr.t >= r.t1c - 600) & (tr.t <= r.t1c + 600)
            paths[r.flight1] = [[float(a), float(b)] for a, b in zip(tr.lat[m][::10], tr.lon[m][::10])]
        label = ("along the aircraft's path" if not two else "best" if k == 0 else "plausible" if top - r.score <= 3 else "less likely")
        rows.append(dict(rank=k + 1, lat=float(r.lat), lon=float(r.lon), score=float(r.score), score1=float(r.score1),
                         score2=float(r.score2), flight1=r.flight1, flight2=r.flight2 or "", label=label,
                         start_utc=datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                         start_local=_local(t0, tz)))
    return rows, paths
