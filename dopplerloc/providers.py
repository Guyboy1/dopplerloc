"""Where the flight data comes from. Every provider returns a DataFrame:
icao24, time (epoch s), lat, lon, alt_m [, typecode]."""
import collections, gzip, http.client, io, json, os, shutil, tarfile, threading, time, urllib.request, urllib.error
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from . import net
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
from .typedb import CACHE

FT = 0.3048


class ProviderError(RuntimeError):
    pass


# ---------------------------------------------------------------- local file
def read_file(path, bbox, t_lo, t_hi, log=print):
    p = str(path)
    df = pd.read_parquet(p) if p.endswith((".parquet", ".pq")) else pd.read_csv(p)
    need = {"icao24", "time", "lat", "lon", "alt_m"}
    if not need <= set(df.columns):
        raise ProviderError(f"{path} needs columns {sorted(need)}; has {sorted(df.columns)}")
    return _clip(df, bbox, t_lo, t_hi)


def _clip(df, bbox, t_lo, t_hi):
    s, w, n, e = bbox
    return df[(df.time >= t_lo) & (df.time <= t_hi) & (df.lat >= s) & (df.lat <= n) &
              (df.lon >= w) & (df.lon <= e)].reset_index(drop=True)


# ---------------------------------------------------------------- OpenSky (UNTESTED here: needs an account)
def fetch_opensky(bbox, t_lo, t_hi, log=print, x=None):
    """Historical state vectors from OpenSky's Trino database (needs username + password of an account with approved data access;
    NOT the REST 'API client' id/secret). OpenSky requires every query to be limited to a partition (one hour here), allows two concurrent
    queries and kills queries over 30 min / 100 GB, so the span is fetched one UTC hour at a time, one after the other."""
    try:
        from pyopensky.trino import Trino
    except ImportError as e:
        raise ProviderError("pip install pyopensky, then set up your OpenSky login (SETUP_GUIDE.md, section OpenSky).") from e
    from .tracks import from_opensky
    s, w, n, e = bbox
    first = int(t_lo // 3600) * 3600
    hours = list(range(first, int(t_hi) + 1, 3600))
    log(f"querying OpenSky Trino: {len(hours)} UTC hour(s), one query per hour (can take minutes each) ...")
    trino, parts = Trino(), []
    if x:
        x._unit_t = time.time()
    for i, h in enumerate(hours, 1):
        lo, hi = max(t_lo, h), min(t_hi, h + 3600)
        if hi <= lo:
            continue
        try:
            df = trino.history(pd.Timestamp(lo, unit="s", tz="UTC"), pd.Timestamp(hi, unit="s", tz="UTC"), bounds=(w, s, e, n))
        except Exception as ex:
            msg = str(ex)
            hint = ""
            if any(k in msg.lower() for k in ("auth", "401", "403", "denied", "forbidden", "credential", "login")):
                hint = (" This usually means the login is wrong or your OpenSky data access has not been approved yet. The program needs your account"
                        " USERNAME and PASSWORD (not an API key). See SETUP_GUIDE.md, section OpenSky.")
            elif "scan limit" in msg.lower() or "100gb" in msg.lower():
                hint = " The area is too large for OpenSky's scan limit; use a smaller search box."
            raise ProviderError(f"OpenSky query failed for hour {i}/{len(hours)}: {msg[:300]}.{hint}") from ex
        if df is not None and len(df):
            parts.append(df)
        if x:
            x.hour_done()
        log(f"OpenSky hour {i}/{len(hours)}: {0 if df is None else len(df)} rows")
    if not parts:
        return pd.DataFrame(columns=["icao24", "time", "lat", "lon", "alt_m"])
    return from_opensky(pd.concat(parts, ignore_index=True))


# ---------------------------------------------------------------- transfer statistics (speed + time estimate)
class Transfer:
    """Thread-safe byte/time counters shared with the Job so it can show the real download speed and a time estimate."""
    DAY_BYTES = 3.2e9                                    # first guess for one UTC day of adsb.lol data

    def __init__(self):
        self.lock = threading.Lock()
        self.dl_done = self.dl_total = 0                 # bytes downloaded / announced by the server so far
        self.scan_done = self.scan_total = 0             # bytes of archive read by the scanner / announced
        self.dl_days_left = self.scan_days_left = 0      # whole days not started yet (uncached / all)
        self.day_bytes = self.DAY_BYTES                  # learned from finished days
        self.units_left, self.unit_secs, self._unit_t = 0, None, None   # OpenSky hours
        self.dl_speed = self.scan_speed = None           # bytes/s, smoothed
        self._d = [time.time(), 0, time.time()]          # [mark time, mark bytes, last activity]
        self._s = [time.time(), 0, time.time()]

    @staticmethod
    def _tick(mark, done, old, n):
        now = time.time()
        if now - mark[2] > 3.0:                          # idle gap (searching, between days): restart the measurement
            mark[0], mark[1] = now, done - n
        mark[2] = now
        if now - mark[0] >= 1.0:
            inst = (done - mark[1]) / (now - mark[0])
            mark[0], mark[1] = now, done
            return inst if old is None else 0.7 * old + 0.3 * inst
        return old

    def add_dl(self, n):
        with self.lock:
            self.dl_done += n
            self.dl_speed = self._tick(self._d, self.dl_done, self.dl_speed, n)

    def add_scan(self, n):
        with self.lock:
            self.scan_done += n
            self.scan_speed = self._tick(self._s, self.scan_done, self.scan_speed, n)

    def announce(self, total, already=0):
        with self.lock:
            self.dl_total += total
            self.dl_done += already
            self.scan_total += total

    def cached(self, size):
        with self.lock:
            self.scan_total += size

    def dl_bytes_left(self):
        with self.lock:
            return max(0, self.dl_total - self.dl_done) + self.dl_days_left * self.day_bytes

    def scan_bytes_left(self):
        with self.lock:
            return max(0, self.scan_total - self.scan_done) + self.scan_days_left * self.day_bytes

    def day_finished(self, nbytes):
        with self.lock:
            if nbytes > 1e8:
                self.day_bytes = nbytes

    def hour_done(self):
        with self.lock:
            now = time.time()
            if self._unit_t is not None:
                d = now - self._unit_t
                self.unit_secs = d if self.unit_secs is None else 0.6 * self.unit_secs + 0.4 * d
            self._unit_t = now
            self.units_left = max(0, self.units_left - 1)


# ---------------------------------------------------------------- adsb.lol daily releases
SUFFIXES = ("aa", "ab", "ac", "ad", "ae")
try:
    import orjson as _fastjson                            # optional: 3-5x faster JSON parsing
    _loads = _fastjson.loads
except ImportError:                                       # pragma: no cover
    _loads = json.loads


def _tag(d, pod="prod"):
    return f"v{d.year}.{d.month:02d}.{d.day:02d}-planes-readsb-{pod}-0"


def _cdir():
    c = CACHE / "adsblol"
    c.mkdir(parents=True, exist_ok=True)
    return c


def cached_day(d, pod="prod"):
    return (CACHE / "adsblol" / f"{_tag(d, pod)}.tar.aa").exists()


def cache_bytes():
    c = CACHE / "adsblol"
    return sum(f.stat().st_size for f in c.glob("*")) if c.exists() else 0


def clear_cache():
    """Delete downloaded adsb.lol archives (and half-finished downloads). Returns bytes freed."""
    c, freed = CACHE / "adsblol", 0
    if c.exists():
        for f in c.glob("*"):
            try:
                n = f.stat().st_size
                f.unlink()
                freed += n
            except OSError:
                pass
    return freed


def free_gb(path=None):
    p = Path(path or CACHE)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def _parse_trace(obj, bbox, t_lo, t_hi):
    """readsb trace JSON -> rows. Format: github.com/wiedehopf/readsb README-json.md ('trace jsons')."""
    s, w, n, e = bbox
    base = obj.get("timestamp")
    if base is None:
        return []
    icao, typ = obj.get("icao", ""), obj.get("t")
    rows = []
    for p in obj.get("trace", []):
        ts = base + p[0]
        if ts < t_lo or ts > t_hi:
            continue
        lat, lon, alt = p[1], p[2], p[3]
        if alt in (None, "ground") or not (s <= lat <= n and w <= lon <= e):
            continue
        if p[6] & 1:                                   # stale position
            continue
        geo = p[10] if len(p) > 10 and p[10] is not None else None
        rows.append((icao.lower(), ts, lat, lon, (geo if geo is not None else alt) * FT, typ))
    return rows


def _parse_batch(raws, bbox, t_lo, t_hi):
    """Unzip + parse + filter a batch of trace files. Runs in worker processes (must stay a module-level function)."""
    rows = []
    for raw in raws:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        try:
            rows += _parse_trace(_loads(raw), bbox, t_lo, t_hi)
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def _workers():
    env = os.environ.get("DOPPLERLOC_WORKERS")
    if env is not None and env.strip().isdigit():
        return int(env)
    return max(1, min(8, (os.cpu_count() or 2) - 1))


class _Chain(io.RawIOBase):
    """Read the .tar.aa/.ab/... parts as one stream. Each part is a Future, so scanning part 1 can start while later parts still download."""
    def __init__(self, futures, x=None):
        self.futs, self.i, self.f, self.x = list(futures), 0, None, x
    def readable(self):
        return True
    def readinto(self, b):
        while True:
            if self.f is None:
                if self.i >= len(self.futs):
                    return 0
                path = self.futs[self.i].result()          # waits until this part is on disk (None = no such part: the end)
                if path is None:
                    self.i = len(self.futs)
                    return 0
                self.f = open(path, "rb")
            n = self.f.readinto(b)
            if n:
                if self.x:
                    self.x.add_scan(n)
                return n
            self.f.close(); self.f = None; self.i += 1


def _scan_stream(fileobj, bbox, t_lo, t_hi, workers=None, progress=None):
    """Walk the tar and parse trace files on several CPU cores (falls back to one core if worker processes cannot start)."""
    workers = _workers() if workers is None else workers
    pool, pending, batch, rows = None, collections.deque(), [], []
    if workers > 1:
        try:
            pool = ProcessPoolExecutor(workers)
        except Exception:
            pool = None

    def drain(limit):
        while len(pending) > limit:
            fut, raws = pending.popleft()
            try:
                rows.extend(fut.result())
            except Exception:                                 # a worker died: do this batch here instead
                rows.extend(_parse_batch(raws, bbox, t_lo, t_hi))

    def submit():
        nonlocal batch
        if not batch:
            return
        if pool is None:
            rows.extend(_parse_batch(batch, bbox, t_lo, t_hi))
        else:
            try:
                pending.append((pool.submit(_parse_batch, batch, bbox, t_lo, t_hi), batch))
            except Exception:
                rows.extend(_parse_batch(batch, bbox, t_lo, t_hi))
            drain(2 * workers)
        batch = []

    try:
        with tarfile.open(fileobj=io.BufferedReader(fileobj, 1 << 20), mode="r|") as tf:
            for k, m in enumerate(tf):
                if not (m.isfile() and "trace_full" in m.name):
                    continue
                batch.append(tf.extractfile(m).read())
                if len(batch) >= 200:
                    submit()
                if progress and k % 2000 == 0:
                    progress(k)
        submit()
        drain(0)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    return rows


def _read_tar_parts(parts, bbox, t_lo, t_hi, progress=None, workers=None):
    """Scan finished part files (a list of paths)."""
    done = []
    for p in parts:
        f = Future(); f.set_result(p); done.append(f)
    return _scan_stream(_Chain(done), bbox, t_lo, t_hi, workers, progress)


def _download(url, dest, x=None, stop=None):
    """Download url -> dest (a Path). Resumes a half-finished .part file. Returns dest, or None when the server says 404."""
    if dest.exists():
        if x:
            x.cached(dest.stat().st_size)
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    have, announced = (tmp.stat().st_size if tmp.exists() else 0), False
    for attempt in range(5):
        headers = {"User-Agent": "dopplerloc"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            r = net.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                tmp.unlink(missing_ok=True)
                return None
            if e.code == 416 and have:                       # we already have all of it
                os.replace(tmp, dest)
                if x and not announced:
                    x.announce(have, have)
                return dest
            raise
        except (urllib.error.URLError, OSError):
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        try:
            with r:
                if have and getattr(r, "status", 200) != 206:   # server ignored the Range header: start again
                    if x:
                        x.add_dl(-have)
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                if x and not announced:
                    x.announce(total, have)
                    announced = True
                with open(tmp, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 20):
                        if stop is not None and stop.is_set():
                            raise ProviderError("stopped")
                        f.write(chunk)
                        have += len(chunk)
                        if x:
                            x.add_dl(len(chunk))
            os.replace(tmp, dest)
            return dest
        except (OSError, http.client.HTTPException):
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
            have = tmp.stat().st_size if tmp.exists() else 0     # resume from what reached the disk
    raise ProviderError("download failed")


def _fetch_day(d, bbox, t_lo, t_hi, x, low_storage, pod, log):
    """One UTC day: download all parts at the same time, scan while they arrive, optionally delete the archive afterwards."""
    tag, repo = _tag(d, pod), f"adsblol/globe_history_{d.year}"
    cdir = _cdir()
    stop = threading.Event()
    log(f"adsb.lol {d}: downloading ({'one day at a time, deleted afterwards' if low_storage else 'kept in the cache folder'}) ...")
    paths = [cdir / f"{tag}.tar.{suf}" for suf in SUFFIXES]
    with ThreadPoolExecutor(len(SUFFIXES)) as ex:
        futs = [ex.submit(_download, f"https://github.com/{repo}/releases/download/{tag}/{tag}.tar.{suf}", pth, x, stop)
                for suf, pth in zip(SUFFIXES, paths)]
        try:
            if futs[0].result() is None:
                raise ProviderError(f"no adsb.lol release found for {d} (tried {repo}, {tag}). "
                                    "That archive may not exist for this date; try another source.")
            log(f"adsb.lol {d}: scanning traces (on {max(1, _workers())} CPU core(s)) while the rest downloads ...")
            rows = _scan_stream(_Chain(futs, x), bbox, t_lo, t_hi)
        finally:
            stop.set()
    have = [pth for pth in paths if pth.exists()]
    if x:
        x.day_finished(sum(p.stat().st_size for p in have))
    if low_storage:
        for pth in have + [q.with_suffix(q.suffix + ".part") for q in paths]:
            try:
                pth.unlink()
            except OSError:
                pass
        log(f"adsb.lol {d}: archive deleted (low-storage mode)")
    return rows


def fetch_adsblol(bbox, t_lo, t_hi, log=print, progress=None, pod="prod", low_storage=False, x=None, memo=None):
    """Each needed UTC day (about 3 GB) is downloaded (parts in parallel), scanned and, in low-storage mode, deleted before the next.
    Rows for a day are remembered in `memo` so two time windows that share a UTC day do not download it twice."""
    x = x or Transfer()
    days, d = [], datetime.fromtimestamp(t_lo, timezone.utc).date()
    while d <= datetime.fromtimestamp(t_hi, timezone.utc).date():
        days.append(d); d += timedelta(days=1)
    out = []
    for d in days:
        key = (d, tuple(round(v, 3) for v in bbox), pod)
        if memo is not None and key in memo:
            log(f"adsb.lol {d}: already scanned in this run, reusing")
            out += memo[key]
            continue
        with x.lock:
            x.scan_days_left = max(0, x.scan_days_left - 1)
            if not cached_day(d, pod):
                x.dl_days_left = max(0, x.dl_days_left - 1)
        day_lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() - 3600
        rows = _fetch_day(d, bbox, day_lo, day_lo + 86400 + 7200, x, low_storage, pod, log)
        if memo is not None:
            memo[key] = rows
            while len(memo) > 3:                              # windows are processed in order; keep only the recent days
                memo.pop(next(iter(memo)))
        out += rows
        if progress:
            progress(x.dl_done, max(x.dl_total, 1))
    df = pd.DataFrame(out, columns=["icao24", "time", "lat", "lon", "alt_m", "typecode"])
    return _clip(df, bbox, t_lo, t_hi) if len(df) else df


# ---------------------------------------------------------------- dispatcher
def utc_days(windows):
    days = set()
    for lo, hi in windows:
        d = datetime.fromtimestamp(lo, timezone.utc).date()
        while d <= datetime.fromtimestamp(hi, timezone.utc).date():
            days.add(d); d += timedelta(days=1)
    return sorted(days)


def estimate(source, windows):
    t0, t1 = min(a for a, _ in windows), max(b for _, b in windows)
    days = (datetime.fromtimestamp(t1, timezone.utc).date() - datetime.fromtimestamp(t0, timezone.utc).date()).days + 1
    if source == "adsblol":
        return dict(gb=3.2 * days, days=days, note=f"adsb.lol: about {3.2 * days:.0f} GB to download for {days} UTC day(s).")
    if source == "opensky":
        return dict(gb=0, days=days, note=f"OpenSky: fetched one UTC hour per query ({days} day(s) span); needs approved data access, large areas/long spans are slow and may be refused.")
    return dict(gb=0, days=days, note="")


def fetch(source, bbox, t_lo, t_hi, file_path=None, log=print, progress=None, low_storage=False, x=None, memo=None):
    if source == "file":
        if not file_path:
            raise ProviderError("choose a flight-data file (parquet/csv)")
        return read_file(file_path, bbox, t_lo, t_hi, log)
    if source == "opensky":
        return fetch_opensky(bbox, t_lo, t_hi, log, x)
    if source == "adsblol":
        return fetch_adsblol(bbox, t_lo, t_hi, log, progress, low_storage=low_storage, x=x, memo=memo)
    raise ProviderError(f"unknown data source {source!r}")
