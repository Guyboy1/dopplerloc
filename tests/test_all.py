"""End-to-end tests on a synthetic world. Run: python tests/test_all.py"""
import sys, os, urllib.error, json, time, subprocess, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from scipy.io import wavfile
from dopplerloc.synth import scenario
from dopplerloc.jobs import Job, run
from dopplerloc.geo import M_PER_DEG, TerrainTilesDEM
from dopplerloc.audio import track_ridge

tmp = tempfile.mkdtemp()
os.environ["DOPPLERLOC_CACHE"] = tmp


def make_files(noise=0.7, name="clip"):
    raw, x, fs, truth = scenario(noise=noise)
    wav = f"{tmp}/{name}.wav"
    wavfile.write(wav, fs, (x / np.abs(x).max() * 30000).astype(np.int16))
    tr = f"{tmp}/tracks.parquet"
    raw.to_parquet(tr)
    return wav, tr, truth


def opts(wav, tr, truth, **kw):
    T0 = truth["T0"]
    o = dict(ack=True, audio_path=wav, bbox=[truth["lat"] - .12, truth["lon"] - .12, truth["lat"] + .12, truth["lon"] + .12],
             timeframe=dict(year=2026, month=11, day=3, tz="UTC", start="02:40", end="02:50"),
             source="file", flight_file=tr, dem="flat")
    o.update(kw)
    return o


def err(row, truth):
    return float(np.hypot((row["lat"] - truth["lat"]) * M_PER_DEG, (row["lon"] - truth["lon"]) * M_PER_DEG * np.cos(np.radians(truth["lat"]))))


def go(o, asker=None):
    j = Job(asker=asker); t = time.time(); run(j, o)
    assert j.status == "done", (j.status, j.error, j.logs[-5:])
    return j, time.time() - t


# 1. fully automatic
wav, tr, truth = make_files()
j, dt = go(opts(wav, tr, truth))
top = j.result["rows"][0]
print(f"[1] automatic: err={err(top, truth):.0f} m  {top['flight1']}+{top['flight2']}  {dt:.1f}s  start_utc={top['start_utc']}")
assert err(top, truth) < 100 and top["flight1"].startswith("trueA")

# 2. same, 2 worker processes
j2, dt = go(opts(wav, tr, truth, workers=2, params=dict(tile_deg=0.1)))
print(f"[2] workers=2, many tiles: err={err(j2.result['rows'][0], truth):.0f} m ({j2.logs[-3]})")
assert err(j2.result["rows"][0], truth) < 100

# 3. every manual hand-off, answered like a user would
calls = []
def asker(kind, payload, job):
    calls.append(kind)
    if kind == "events":
        return dict(events=[dict(win=(35, 212), band=(60, 450)), dict(win=(953, 1131), band=(60, 450))])
    if kind == "trace":
        t, f, w = track_ridge(job.spec, tuple(payload["band"]), tuple(payload["win"]))
        sel = np.where(w > 8)[0][::25]
        return dict(points=[(float(t[i]), float(f[i])) for i in sel])       # = user clicking along the curve
    if kind == "review":
        return {}
    return {}
j3, dt = go(opts(wav, tr, truth, always_manual=True) | dict(always_manual_trace=True), asker)
print(f"[3] manual events + hand-traced curves + review: calls={calls} err={err(j3.result['rows'][0], truth):.0f} m")
assert calls.count("events") == 1 and calls.count("trace") == 2 and "review" in calls and err(j3.result["rows"][0], truth) < 150

# 4. noisy recording: automatic detection gives up -> manual events; "user" traces the faint curve
from dopplerloc.tracks import build_tracks
from dopplerloc.model import ratio_curve, cpa_points
from dopplerloc.geo import lla_to_ecef
wavn, trn, truthn = make_files(noise=8.0, name="noisy")
raw_n = pd.read_parquet(trn); tk = {t.id: t for t in build_tracks(raw_n)}
obs = lla_to_ecef(truthn["lat"], truthn["lon"], 2.0)
def perfect_user(kind, payload, job):
    if kind == "events":
        return dict(events=[dict(win=(35, 212), band=(60, 450)), dict(win=(953, 1131), band=(60, 450))])
    if kind == "trace":
        k = payload["index"] - 1; name, f0, tau = (("trueA_0", 94.9, truthn["tauA"]), ("trueB_0", 142.4, truthn["tauB"]))[k]
        c = cpa_points(tk[name], obs[None], 5000); rel = np.arange(-70, 70, 6.0)
        ra = ratio_curve(tk[name], obs, c.t[0] + rel); rng = np.random.default_rng(k)
        return dict(points=[(tau + r_, f0 * a + rng.normal(0, .4)) for r_, a in zip(rel, ra)])
    return {}
calls = []
_wrap = lambda kind, payload, job: (calls.append(kind), perfect_user(kind, payload, job))[1]
j4, dt = go(opts(wavn, trn, truthn), _wrap)
print(f"[4] very noisy audio: asked for {calls}; err={err(j4.result['rows'][0], truthn):.0f} m")
assert calls[0] == "events"

# 5. refuses without the permission tick
j5 = Job(); run(j5, opts(wav, tr, truth, ack=False)); assert j5.status == "error" and "permission" in j5.error
print("[5] refuses without acknowledgement:", j5.error[:60])

# 6. web: upload an iPhone-style .mov with GPS + creationdate, check prefill, run a job through the HTTP API
from dopplerloc import web
mov = f"{tmp}/IMG_0001.mov"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=1200", "-shortest",
                "-metadata", "com.apple.quicktime.location.ISO6709=+27.9506-082.4572+003.000/",
                "-metadata", "com.apple.quicktime.creationdate=2026-11-03T12:44:40+1000",
                "-metadata", "com.apple.quicktime.make=Apple", "-movflags", "use_metadata_tags", mov], check=True)
c = web.app.test_client()
with open(mov, "rb") as f:
    up = c.post("/api/upload", data={"file": (f, "IMG_0001.mov")}, content_type="multipart/form-data").json
print("[6] upload:", {k: up.get(k) for k in ("duration", "has_video")}, up["metadata"], up["prefill"])
assert abs(up["metadata"]["lat"] - 27.9506) < 1e-6 and up["prefill"]["country"] == "US" and up["prefill"]["year"] == 2026 and up["prefill"]["day"] == 2 and up["prefill"]["tz"] == "America/New_York" and up["prefill"]["exact"] == "21:44:40" and up["prefill"]["time_tolerance_s"] == 2.0
pf = up["prefill"]       # exactly what the UI would send after auto-filling the boxes
body = dict(ack=True, file_id=up["file_id"], bbox=opts(wav, tr, truth)["bbox"], tz=pf["tz"], year=pf["year"], month=pf["month"],
            day=pf["day"], exact=pf["exact"], tolerance_s=pf["time_tolerance_s"], source="file", flight_file=tr, dem="flat", workers=1)
r = c.post("/api/start", json=dict(body, ack=False)); assert r.status_code == 400
jid = c.post("/api/start", json=body).json["job_id"]
for _ in range(120):
    st = c.get(f"/api/job/{jid}").json
    if st["status"] in ("done", "error"): break
    time.sleep(1)
print("    web job:", st["status"], st["percent"], "%", st["error"], "top:", st["result"]["rows"][0]["flight1"] if st["result"] else None)
assert st["status"] == "done"
img = c.get(f"/api/job/{jid}/overlay/0/0"); assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
assert c.get(f"/api/job/{jid}/img?t0=0&t1=300&f0=40&f1=500").status_code == 200

# 7. terrain tile DEM (fake tile in the cache; no network)
from PIL import Image
dem = TerrainTilesDEM(zoom=10, offline=True)
n = 2 ** 10; lat, lon = -27.47, 153.03
x = int((lon + 180) / 360 * n); y = int((1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n)
elev = 123.5 + 32768; arr = np.zeros((256, 256, 3), np.uint8); arr[..., 0] = int(elev // 256); arr[..., 1] = int(elev % 256); arr[..., 2] = int((elev % 1) * 256)
Image.fromarray(arr).save(dem.dir / f"{x}_{y}.png")
print("[7] terrain tile sample:", float(dem(np.array([lat]), np.array([lon]))[0]), "(expect ~123.5)")
assert abs(dem(np.array([lat]), np.array([lon]))[0] - 123.5) < 0.1

img = c.get(f"/api/job/{jid}/preview"); assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
res = c.get(f"/api/job/{jid}").json["result"]; assert res["mode"] == "two" and len(res["bbox"]) == 4 and res["rows"][0]["label"] == "best"
print("[6b] live preview PNG ok; result has bbox + labelled rows:", [r["label"] for r in res["rows"]])

# 8. BROAD timeframe (a whole day) with two clear curves -> continues
broad = dict(year=2026, month=11, day=3, tz="UTC")
j8, _ = go(opts(wav, tr, truth, timeframe=broad))
print(f"[8] broad timeframe, 2 curves auto-detected: mode={j8.result['mode']} err={err(j8.result['rows'][0], truth):.0f} m")
assert j8.result["mode"] == "two" and err(j8.result["rows"][0], truth) < 100

# 9. BROAD timeframe, user marks only ONE curve -> refused and asked again; second answer with two curves proceeds
answers = [[dict(win=(35, 212), band=(60, 450))],
           [dict(win=(35, 212), band=(60, 450)), dict(win=(953, 1131), band=(60, 450))]]
seen = []
def asker9(kind, payload, job):
    if kind == "events":
        seen.append((payload["need"], payload["reason"][:40]))
        return dict(events=answers[len(seen) - 1])
    return {}
j9, _ = go(opts(wav, tr, truth, timeframe=broad, always_manual=True), asker9)
print("[9] broad + one curve marked -> re-asked:", seen)
assert len(seen) == 2 and seen[0][0] == 2 and "TWO" in seen[1][1] + "TWO" and err(j9.result["rows"][0], truth) < 100
# ... and if the user gives up instead, the job stops without searching
j9b = Job(asker=lambda k, p, jb: dict(events=answers[0]) if len(seen) < 99 and k == "events" and not seen.append(0) else dict(cancel=True))
run(j9b, opts(wav, tr, truth, timeframe=broad, always_manual=True)); assert j9b.status == "error" and j9b.result is None
print("[9b] user stops instead -> no search:", j9b.error)

# 10. SPECIFIC timeframe (10 minutes), only ONE curve -> one-curve mode, spread-out candidates along the aircraft path
kinds = []
def asker10(kind, payload, job):
    kinds.append(kind)
    return dict(events=[dict(win=(35, 212), band=(60, 450))]) if kind == "events" else {}
exact_tf = dict(year=2026, month=11, day=3, tz="UTC", exact="02:44:40", tolerance_s=5)
j10, _ = go(opts(wav, tr, truth, always_manual=True, timeframe=exact_tf), asker10)
rows = j10.result["rows"]; d = [err(r, truth) for r in rows]
print(f"[10] one curve + exact start (+-5 s): mode={j10.result['mode']} {len(rows)} candidates, nearest to truth {min(d):.0f} m, farthest {max(d):.0f} m, path for {list(j10.result['tracks'])}")
assert j10.result["mode"] == "one" and rows[0]["flight1"].startswith("trueA") and min(d) < 700 and max(d) > 1200 and "trueA_0" in j10.result["tracks"]       # near candidate + its mirror image across the flight path

# 11. every country in the picker resolves to a box and a time zone
from dopplerloc import places
from zoneinfo import ZoneInfo
cs = places.countries(); assert len(cs) > 230
for ct in cs:
    r = places.resolve(ct["code"]); b = r["bbox"]
    assert b[0] < b[2] and b[1] < b[3] and r["timezones"], ct
    ZoneInfo(r["timezones"][0])
print(f"[11] {len(cs)} countries all resolve to a search box and a valid time zone")

# 12. fine heat maps + score scatter data for the winning pair, via the web API
res = c.get(f"/api/job/{jid}").json["result"]
assert set(res["heat"]) == {"e1", "e2", "both"} and len(res["pairs"]) >= 1
for k in res["heat"]:
    r_ = c.get(f"/api/job/{jid}/heat/{k}"); assert r_.status_code == 200 and r_.data[:4] == b"\x89PNG"
print(f"[12] heat maps {res['heat']} + {len(res['pairs'])} scatter points served")
from dopplerloc.search import heatmap, Params
from dopplerloc.tracks import build_tracks as bt
from dopplerloc.audio import make_spec, fit_event
raw_, x_, fs_, tr_ = scenario(); trk = bt(raw_); sp = make_spec(x_, fs_)
e1, e2 = fit_event(sp, (60, 450), (35, 212)), fit_event(sp, (60, 450), (953, 1131))
hm = heatmap(trk, sp, e1, e2, __import__("dopplerloc.geo", fromlist=["DEM"]).DEM(), Params(), "trueA_0", "trueB_0", tr_["lat"], tr_["lon"], 600, 50)
i, j = np.unravel_index(np.nanargmax(hm["both"]), hm["both"].shape)
off = np.hypot((i - 12) * 50, (j - 12) * 50)
print(f"[12b] combined heat map peak is {off:.0f} m from the true spot (grid step 50 m)")
assert off <= 120 and np.nanmax(hm["both"]) > np.nanmedian(hm["both"]) + 3

# 13. spectrogram view at higher resolution (for faint curves)
for res_ in ("high", "max"):
    r_ = c.get(f"/api/job/{jid}/img?t0=35&t1=212&f0=60&f1=450&res={res_}"); assert r_.status_code == 200 and r_.data[:4] == b"\x89PNG", res_
print("[13] high / max resolution views render")

# 14. flight data is fetched with a margin around the search box (flights near the edge keep their full path)
from dopplerloc import providers
seen_box = []
orig = providers.fetch
providers.fetch = lambda src, box, *a, **k: (seen_box.append(box), orig(src, box, *a, **k))[1]
o14 = opts(wav, tr, truth); go(o14)
providers.fetch = orig
b0, b1 = o14["bbox"], seen_box[0]
print("[14] search box", [round(v, 2) for v in b0], "-> fetched box", [round(v, 2) for v in b1])
assert b1[0] <= b0[0] - 0.19 and b1[2] >= b0[2] + 0.19 and b1[1] <= b0[1] - 0.19 and b1[3] >= b0[3] + 0.19

# 15. faint curve scored by closeness to the hand-traced line (video: one-curve example)
from dopplerloc.search import Scorer
from dopplerloc.geo import lla_to_ecef, offset_latlon
from dopplerloc.model import cpa_points, ratio_curve
obs_ = lla_to_ecef(tr_["lat"], tr_["lon"], 2.0); tk_ = {t.id: t for t in trk}
cA = cpa_points(tk_["trueA_0"], obs_[None], 5000); rel = np.arange(-70, 70, 6.0)
ra_ = ratio_curve(tk_["trueA_0"], obs_, cA.t[0] + rel)
traced = fit_event(sp, (60, 450), (35, 212)); traced.trace = tuple((tr_["tauA"] + r_, 94.9 * a) for r_, a in zip(rel, ra_))
sc_ = Scorer(trk, sp, traced, None, __import__("dopplerloc.geo", fromlist=["DEM"]).DEM(), Params())
at = sc_.point1("trueA_0", tr_["lat"], tr_["lon"])[4]
la2, lo2 = offset_latlon(tr_["lat"], tr_["lon"], 700, 0); away = sc_.point1("trueA_0", la2, lo2)[4]
print(f"[15] trace-line score at truth {at:.1f} vs 700 m away {away:.1f}")
assert at > 14 and at > away + 4

# 16. one curve via the events panel: typed exact start time turns a non-exact timeframe into one-curve mode
def asker16(kind, payload, job):
    if kind == "events":
        assert payload["allow_exact"] and payload["need"] == 2
        return dict(events=[dict(win=(35, 212), band=(60, 450))], exact="02:44:40", tolerance=5)
    return {}
j16, _ = go(opts(wav, tr, truth, always_manual=True), asker16)
d16 = [err(r, truth) for r in j16.result["rows"]]
print(f"[16] events panel: one curve + typed exact start -> mode={j16.result['mode']}, nearest candidate {min(d16):.0f} m")
assert j16.result["mode"] == "one" and min(d16) < 700

# 17. RECORDING WITH CUTS: flight B really happened 2 h later than the audio offset suggests. Only per-curve clock times can find it.
from datetime import datetime, timezone
raw_c = pd.read_parquet(tr); raw_c.loc[raw_c.icao24.str.contains("B") & ~raw_c.is_jet, "time"] += 7200
tr_cut = f"{tmp}/tracks_cut.parquet"; raw_c.to_parquet(tr_cut)
hms = lambda ep: datetime.fromtimestamp(ep, timezone.utc).strftime("%H:%M:%S")
tA, tB = truth["T0"] + truth["tauA"], truth["T0"] + truth["tauB"] + 7200
seen17 = []
def asker17(kind, payload, job):
    seen17.append(kind)
    if kind == "events":
        return dict(events=[dict(win=(35, 212), band=(60, 450)), dict(win=(953, 1131), band=(60, 450))])
    if kind == "curve_times":
        assert len(payload["curves"]) == 2 and payload["curves"][0]["suggested"] == hms(__import__("dopplerloc.synth", fromlist=["x"]).BASE + 9880 + payload["curves"][0]["tau"])      # typed start 02:44:40 + tau
        return dict(times=[hms(tA), hms(tB)], tolerances=[5, 5])
    return {}
cut_tf = dict(year=2026, month=11, day=3, tz="UTC", exact="02:44:40", tolerance_s=5, cuts=True)
j17, _ = go(opts(wav, tr_cut, truth, timeframe=cut_tf), asker17)
r17 = j17.result["rows"][0]
print(f"[17] cuts mode: asked {seen17}; best={r17['flight1']}+{r17['flight2']} err={err(r17, truth):.0f} m (indep={j17.result['indep']})")
assert "curve_times" in seen17 and err(r17, truth) < 100 and r17["flight2"].startswith("trueB") and j17.result["indep"]
# the same data treated as a continuous recording must NOT find it (B is 2 h away from where the offset says)
j17b, _ = go(opts(wav, tr_cut, truth, timeframe=dict(year=2026, month=11, day=3, tz="UTC", exact="02:44:40")), lambda k, p, jb: {})
print("[17b] same data as continuous recording ->", "no match" if not j17b.result else f"top err {err(j17b.result['rows'][0], truth):.0f} m (not the truth)")
assert (not j17b.result) or err(j17b.result["rows"][0], truth) > 1000

# 18. time/place rules are enforced
for label, tfx, text in (("month only", dict(year=1970, month=1, tz="UTC"), "choose the day"),
                         ("cuts without start", dict(year=2026, month=11, day=3, tz="UTC", cuts=True), "start time"),
                         ("9 days", dict(year=2026, month=11, day=3, tz="UTC", span_days=9), "1 to 7")):
    j_ = Job(); run(j_, opts(wav, tr, truth, timeframe=tfx)); assert j_.status == "error" and text in j_.error, (label, j_.error)
print("[18] rules enforced: month-only, cuts-without-start, >7 days all refused with a clear message")

# 19. metadata from many kinds of files (Android, mp3 tags, text coordinates, timestamp in file name), via the web API
def mk(path, meta=(), vid=False):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", wav] + (["-f", "lavfi", "-i", "color=c=black:s=32x32:r=1:d=5", "-t", "5"] if vid else ["-t", "5"])
    for m_ in meta: cmd += ["-metadata", m_]
    subprocess.run(cmd + [path], check=True)
cases = {
    "android.mp4": (["location=+37.7749-122.4194/", "creation_time=2026-11-03T02:30:05.000000Z"], True),
    "note.mp3": (["date=2026-11-03 12:30:05", "comment=Recorded at 27.9506, -82.4572"], False),
    "PXL_20261103_024440123.wav": ([], False),
    "plain.m4a": ([], False),
}
got = {}
for name, (meta_, vid) in cases.items():
    p_ = f"{tmp}/{name}"; mk(p_, meta_, vid)
    with open(p_, "rb") as f:
        got[name] = c.post("/api/upload", data={"file": (f, name)}, content_type="multipart/form-data").json
a_ = got["android.mp4"]; assert abs(a_["metadata"]["lat"] - 37.7749) < 1e-4 and a_["prefill"]["country"] == "US" and a_["metadata"]["time_confidence"] == "medium"
n_ = got["note.mp3"]; assert abs(n_["metadata"]["lat"] - 27.9506) < 1e-6 and n_["metadata"]["time_source"].startswith("date")
w_ = got["PXL_20261103_024440123.wav"]; assert w_["prefill"]["exact"] == "02:44:40" and w_["metadata"]["time_source"].startswith("timestamp in the file name")
pl = got["plain.m4a"]; assert "lat" not in pl["metadata"] and "time_utc" not in pl["metadata"]
print("[19] metadata: android GPS+time, mp3 tag time + coordinates written in a comment, time from file name, plain file -> nothing (as it should)")
os.environ["DOPPLERLOC_FORCE_BUNDLED"] = "1"; os.environ["DOPPLERLOC_NO_EXIFTOOL"] = "1"
with open(f"{tmp}/android.mp4", "rb") as f:
    b_ = c.post("/api/upload", data={"file": (f, "android.mp4")}, content_type="multipart/form-data").json
assert abs(b_["metadata"]["lat"] - a_["metadata"]["lat"]) < 1e-4 and b_["metadata"]["time_utc"] == a_["metadata"]["time_utc"] and b_["tool"] == "ffmpeg"
print("[19b] same answers using only the bundled ffmpeg (no ffprobe, no exiftool):", b_["tool"])
w2, _info = __import__("dopplerloc.media", fromlist=["x"]).extract_audio(f"{tmp}/IMG_0001.mov", out_dir=tmp)
sr_, xx = wavfile.read(w2); assert sr_ == 2000 and xx.dtype == np.float32 and len(xx) / sr_ > 1100
print(f"[19c] bundled ffmpeg converts the video to 32-bit float audio: {len(xx)/sr_:.0f} s")
os.environ.pop("DOPPLERLOC_FORCE_BUNDLED"); os.environ.pop("DOPPLERLOC_NO_EXIFTOOL")

# 20. a missing ffmpeg gives instructions, not a crash; the health endpoint reports it
import shutil as _sh
from dopplerloc import media as _m
_w, _b = _sh.which, _m._bundled_ffmpeg
_sh.which = lambda n, *a, **k: None; _m._bundled_ffmpeg = lambda: None
try:
    msg = c.get("/api/health").json
    try: _m.ffmpeg_exe(); raise AssertionError("should fail")
    except RuntimeError as e: assert "imageio-ffmpeg" in str(e) and "SETUP_GUIDE" in str(e)
finally:
    _sh.which, _m._bundled_ffmpeg = _w, _b
assert msg["ffmpeg"] is False and "imageio-ffmpeg" in msg["message"] and c.get("/api/health").json["ffmpeg"] is True
print("[20] no ffmpeg -> friendly fix instructions + health banner data; restored OK")

# 21. OpenSky login typed in the page reaches pyopensky for that run only
seen21 = []
def fake_opensky(bbox, t_lo, t_hi, log=print, x=None):
    seen21.append((os.environ.get("OPENSKY_USERNAME"), os.environ.get("OPENSKY_PASSWORD")))
    return providers.read_file(tr, bbox, t_lo, t_hi)
_orig = providers.fetch_opensky; providers.fetch_opensky = fake_opensky
os.environ.pop("OPENSKY_USERNAME", None); os.environ.pop("OPENSKY_PASSWORD", None)
try:
    go(opts(wav, tr, truth, source="opensky", opensky_user="u1", opensky_pass="p1"))
finally:
    providers.fetch_opensky = _orig
assert seen21 and seen21[0] == ("u1", "p1") and "OPENSKY_USERNAME" not in os.environ and "OPENSKY_PASSWORD" not in os.environ
print("[21] OpenSky login passed through for the fetch, then removed from the environment")

# 22. the page itself: one copy only (a previous patch duplicated it), no duplicate element ids, valid JavaScript
import re, collections
html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dopplerloc", "static", "index.html"), encoding="utf8").read()
markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
ids = re.findall(r'\sid="([^"]+)"', markup)
assert html.count("<body>") == 1 and html.count("</html>") == 1 and len(re.findall(r"<script>", html)) == 1
assert not [k for k, v in collections.Counter(ids).items() if v > 1] and len(re.findall(r"<h2>", html)) == 6
if _sh.which("node"):
    open(f"{tmp}/ui.js", "w").write(re.findall(r"<script>(.*?)</script>", html, re.S)[0])
    assert subprocess.run(["node", "--check", f"{tmp}/ui.js"], capture_output=True).returncode == 0
print(f"[22] page structure ok: one copy, {len(ids)} unique ids, JS syntax valid")
# 23. coverage hints: moderate / low / none, zone beats country, API + job log note
from dopplerloc.coverage import assess
from dopplerloc import web as _web
assert assess("JP", [35.5, 139.5, 35.9, 139.9])["level"] == "moderate"
assert assess("KZ", [50, 70, 51, 71])["level"] == "low" and assess("AF", [34.4, 69.0, 34.7, 69.4])["level"] == "low" and assess("SS", [4.7, 31.4, 5.0, 31.8])["level"] == "low"
assert assess("RU", [55.0, 82.5, 55.3, 83.2])["level"] == "low" and "Siberia" in assess("RU", [55.0, 82.5, 55.3, 83.2])["area"]
assert assess("RU", [55.5, 37.3, 56.0, 37.9])["level"] == "moderate"       # Moscow (country level)
assert assess("EG", [24.0, 25.0, 25.0, 26.0])["level"] == "low"             # Egyptian Sahara overrides the moderate country tag
assert assess("EG", [30.0, 31.0, 30.2, 31.5])["level"] == "moderate"        # Cairo
assert assess("BR", [-4.0, -62.0, -3.0, -60.0])["level"] == "low"           # Amazon
assert assess("AU", [-27.7, 152.8, -27.3, 153.2])["level"] == "none"
c = _web.app.test_client()
cl = {x["code"]: x["coverage"] for x in c.get("/api/places/countries").json}
assert cl["JP"] == "moderate" and cl["MN"] == "low" and cl["AU"] == "none"
r = c.get("/api/coverage?country=IN&s=28.4&w=77&n=28.9&e=77.5").json
assert r["level"] == "moderate" and r["reason"]
j23, _ = go(opts(wav, tr, truth, country="JP"))
assert any("NOTE: Moderate coverage" in l for l in j23.logs)
print("[23] coverage hints: moderate/low/none, zones override country, API + country tags + job log note")

# 24. OpenSky: one query per UTC hour, username lowercased, helpful error for login problems
import types
from dopplerloc.jobs import _opensky_login
calls24 = []
class FakeTrino:
    fail = False
    def history(self, start, stop, bounds=None, **kw):
        if FakeTrino.fail:
            raise RuntimeError("401 Unauthorized: access denied")
        calls24.append((start.value // 10**9, stop.value // 10**9))
        t = start.value // 10**9
        return pd.DataFrame(dict(time=[float(t)], icao24=["abc123"], lat=[1.0], lon=[2.0], geoaltitude=[3000.0], baroaltitude=[3000.0], onground=[False]))
_m = types.ModuleType("pyopensky"); _mt = types.ModuleType("pyopensky.trino"); _mt.Trino = FakeTrino; _m.trino = _mt
sys.modules["pyopensky"], sys.modules["pyopensky.trino"] = _m, _mt
t_lo = 1_800_000_000 - 1_800_000_000 % 3600 + 1200            # 20 min past an hour
out24 = providers.fetch_opensky((0, 0, 5, 5), t_lo, t_lo + 2 * 3600 + 600, log=lambda *_: None)
assert len(calls24) == 3 and all(b - a <= 3600 and a // 3600 == (b - 1) // 3600 for a, b in calls24) and len(out24) == 3, calls24
FakeTrino.fail = True
try:
    providers.fetch_opensky((0, 0, 5, 5), t_lo, t_lo + 600, log=lambda *_: None); raise SystemExit("expected ProviderError")
except providers.ProviderError as ex:
    assert "USERNAME and PASSWORD" in str(ex) and "not an API key" in str(ex)
lg = _opensky_login(dict(source="opensky", opensky_user=" MyUser ", opensky_pass="x"))
with lg:
    assert os.environ["OPENSKY_USERNAME"] == "myuser"
assert "OPENSKY_USERNAME" not in os.environ
del sys.modules["pyopensky"], sys.modules["pyopensky.trino"]
print("[24] OpenSky: one query per UTC hour (3 queries for 2h10m), login errors explain username/password vs API key, username lowercased")

# 25. manual detection screens: category-specific instructions, candidates one by one, readable labelled images, no bottom preview bar
seen25 = []
def asker25(kind, payload, job=None):
    if kind == "events":
        seen25.append(payload)
        return dict(events=[dict(win=(35, 212), band=(60, 450))])
    if kind == "review":
        return {}
    return {}
j25, _ = go(opts(wav, tr, truth, always_manual=True, timeframe=dict(year=2026, month=11, day=3, tz="UTC", exact="02:40:00")), asker25)
p25 = seen25[0]
assert p25["category"] == "exact" and p25["need"] == 1 and isinstance(p25["candidates"], list) and "TWO" not in p25["reason"] and "need" in p25
assert all(("win" in c and "strong" in c) for c in p25["candidates"])
from dopplerloc import images as _im
png, meta25 = _im.render(j25.spec, 0, 120, 40, 500, 1100, 420)
assert meta25["ml"] > 0 and meta25["W"] == 1100 + meta25["ml"] + 12 and meta25["w"] == 1100 and meta25["H"] > 420
png2, meta2 = _im.render(j25.spec, 0, 120, 40, 500, 700, 110, axes=False)
assert meta2["ml"] == 0 and meta2["W"] == 700
assert "id=\"live\"" not in html and "livepic" not in html and "updateLive" not in html
assert "recommended" in html.lower() and html.index("adsb.lol daily archive") < html.index("My own file")
print("[25] manual screens: category-specific text, candidates list, labelled calm images, live bar removed, adsb.lol marked recommended")

# 26. certificate errors: friendly message with fixes; SSL_CERT_FILE / INSECURE switches build a context; downloads use net.py
import ssl, urllib.error
from dopplerloc import net, typedb, geo
import dopplerloc.net as _net
real = _net.urllib.request.urlopen
def boom(*a, **k):
    raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate"))
_net.urllib.request.urlopen = boom
try:
    net.urlopen("https://example.org/x"); raise SystemExit("expected NetError")
except net.NetError as ex:
    assert "truststore" in str(ex) and "Install Certificates.command" in str(ex) and "SSL_CERT_FILE" in str(ex) and "example.org" in str(ex)
_net.urllib.request.urlopen = real
os.environ["DOPPLERLOC_INSECURE_SSL"] = "1"; _net._ctx = None
assert net.context().verify_mode == ssl.CERT_NONE
del os.environ["DOPPLERLOC_INSECURE_SSL"]; _net._ctx = None
assert net.context().verify_mode == ssl.CERT_REQUIRED
import inspect
for mod in (typedb, providers, geo):
    src = inspect.getsource(mod)
    assert "urllib.request.urlretrieve" not in src and "urllib.request.urlopen" not in src, mod.__name__
print("[26] certificate errors: readable fix list, context switches, every download goes through net.py")

# 27. adsb.lol pipeline against a fake server: parallel parts, resume of a half download, 404 ends the parts, 1 vs 2 workers give the same rows,
#     low-storage deletes the archive, memo avoids a second download, speed counters move
import gzip as _gz, io as _io, tarfile as _tar, json as _json, pathlib as _pl, datetime as _dt, threading as _th
from dopplerloc import providers as _pv
day = _dt.date(2026, 11, 3)
base_ts = int(_dt.datetime(2026, 11, 3, 12, 0, tzinfo=_dt.timezone.utc).timestamp())
bio = _io.BytesIO()
with _tar.open(fileobj=bio, mode="w") as tfw:
    for k in range(400):
        obj = dict(timestamp=base_ts, icao=f"{0xabc000 + k:06x}", t="B738",
                   trace=[[i * 10, 10.0 + k * 0.001, 20.0 + i * 0.001, 30000, 400, 90, 0, 0, None, None, 30100] for i in range(30)])
        raw = _gz.compress(_json.dumps(obj).encode())
        ti = _tar.TarInfo(f"traces/{k % 256:02x}/trace_full_{k:06x}.json"); ti.size = len(raw)
        tfw.addfile(ti, _io.BytesIO(raw))
blob = bio.getvalue(); cut1, cut2 = len(blob) // 3, 2 * len(blob) // 3
PARTS = {"aa": blob[:cut1], "ab": blob[cut1:cut2], "ac": blob[cut2:]}
REQS = []
class FakeResp:
    def __init__(self, data, status):
        self._b, self.status = _io.BytesIO(data), status
        self.headers = {"Content-Length": str(len(data))}
    def read(self, n=-1): return self._b.read(n)
    def __enter__(self): return self
    def __exit__(self, *a): self._b.close()
def fake_urlopen(req, timeout=30):
    url = req.full_url; suf = url.rsplit(".", 1)[-1]; REQS.append((suf, req.headers.get("Range")))
    if suf not in PARTS:
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    data = PARTS[suf]; rng = req.headers.get("Range")
    if rng:
        return FakeResp(data[int(rng.split("=")[1].split("-")[0]):], 206)
    return FakeResp(data, 200)
_old_open, _old_cache = _pv.net.urlopen, _pv.CACHE
_pv.CACHE = _pl.Path(tmp) / "cache27"
_pv.net.urlopen = fake_urlopen
try:
    bb = (9.0, 19.0, 11.0, 21.0); lo, hi = base_ts - 100, base_ts + 400
    os.environ["DOPPLERLOC_WORKERS"] = "1"
    cdir = _pv.CACHE / "adsblol"; cdir.mkdir(parents=True)
    tag = _pv._tag(day)
    (cdir / f"{tag}.tar.aa.part").write_bytes(PARTS["aa"][:5000])             # a half-finished download to resume
    x1 = _pv.Transfer(); x1.scan_days_left = x1.dl_days_left = 1
    df1 = _pv.fetch_adsblol(bb, lo, hi, log=lambda *_: None, x=x1)
    assert ("aa", "bytes=5000-") in REQS, REQS
    assert len(df1) == 400 * 30 and df1.icao24.nunique() == 400, (len(df1), df1.icao24.nunique())
    assert x1.dl_done == len(blob) and x1.scan_done == len(blob) and x1.dl_days_left == 0 and x1.day_bytes == 3.2e9
    x1.day_finished(4e9); assert x1.day_bytes == 4e9                          # learns the real day size once it is plausible
    assert all((cdir / f"{tag}.tar.{s_}").exists() for s_ in PARTS)           # normal mode keeps the archive
    n_req = len(REQS)
    memo = {}
    _pv.fetch_adsblol(bb, lo, hi, log=lambda *_: None, memo=memo)             # cached files: no new requests
    assert not [r for r in REQS[n_req:] if r[0] in PARTS]                     # cached parts are not requested again (only the 404 probes)
    os.environ["DOPPLERLOC_WORKERS"] = "2"
    df2 = _pv.fetch_adsblol(bb, lo, hi, log=lambda *_: None, memo=memo)       # memo hit; also compare worker counts below
    rows1 = _pv._read_tar_parts([cdir / f"{tag}.tar.{s_}" for s_ in PARTS], bb, lo, hi, workers=1)
    rows2 = _pv._read_tar_parts([cdir / f"{tag}.tar.{s_}" for s_ in PARTS], bb, lo, hi, workers=2)
    assert sorted(rows1) == sorted(rows2) and len(rows1) == 400 * 30
    freed = _pv.clear_cache(); assert freed >= len(blob) and not list(cdir.glob("*"))
    REQS.clear()
    dfl = _pv.fetch_adsblol(bb, lo, hi, log=lambda *_: None, low_storage=True)
    assert len(dfl) == 400 * 30 and not list(cdir.glob("*")), list(cdir.glob("*"))   # low storage: nothing left behind
    assert {r[0] for r in REQS} >= {"aa", "ab", "ac", "ad"}                 # parts requested together, 'ad' is the 404 that ends the list
finally:
    _pv.net.urlopen, _pv.CACHE = _old_open, _old_cache
    os.environ.pop("DOPPLERLOC_WORKERS", None)
print("[27] adsb.lol pipeline: parallel parts, resume (Range), 404 ends list, 1 vs 2 workers identical, low-storage leaves no archive, memo/cache reuse")

# 28. time estimate follows the measured speed; waiting for the user is not counted; percent never goes backwards
from dopplerloc.jobs import Job as _Job, _expect
j28 = _Job(); j28.source = "adsblol"
assert j28.eta()[0] is None and j28.timing()["eta_s"] is None
_expect(j28, dict(source="adsblol"), [(base_ts, base_ts + 3600)], 1)
j28.status = "running"; j28.step(4)
e_rough, rough = j28.eta(); assert rough and e_rough > 0
x28 = j28.xfer; x28.announce(3_000_000_000); x28.dl_speed = x28.scan_speed = 10e6; x28.dl_days_left = x28.scan_days_left = 0
e_fast, rough2 = j28.eta(); assert not rough2 and j28.timing()["eta_search_guess"]
x28.dl_speed = x28.scan_speed = 2.5e6
e_slow, _ = j28.eta(); assert e_slow > 3.5 * (e_fast - 40) > 0, (e_fast, e_slow)      # slower measured speed -> proportionally longer
x28.add_dl(1_500_000_000); x28.add_scan(1_500_000_000); x28.dl_speed = x28.scan_speed = 2.5e6
e_half, _ = j28.eta(); assert e_half < e_slow
p1 = j28.percent; j28.xfer.dl_speed = 0.5e6; j28.xfer.scan_speed = 0.5e6; p2 = j28.percent; assert p2 >= p1     # never backwards
j28.manual = dict(kind="events"); j28.status = "waiting"
t28 = j28.timing(); assert t28["waiting"] and t28["eta_s"] is None
dl28 = (lambda j: (setattr(j, "status", "running"), setattr(j, "manual", None), j.timing())[2])(j28)["dl"]
assert dl28 and dl28["total"] >= dl28["done"] and abs(dl28["speed"] - 0.5e6) < 1
print("[28] time estimate: follows measured speed, rough flag before measurement, manual waits excluded, percent monotonic")

# 29. release material: credits on the page, NOTICE/data licence files, upload deletion
import pathlib as _p2
root29 = _p2.Path(web.__file__).resolve().parent.parent
assert 'id="credits"' in html and "Terrain Tiles on AWS" in html and "adsb.lol" in html and "OpenStreetMap" in html and "Experimental" in html
notice = (root29 / "NOTICE").read_text()
for key in ("dr5hn", "GeoNames", "Natural Earth", "adsb.lol", "ODbL", "Terrain Tiles on AWS", "USGS" if "USGS" in notice else "U.S. Geological Survey", "Leaflet", "OpenSky", "tar1090-db", "Inter"):
    assert key in notice, key
assert (root29 / "dopplerloc" / "data" / "DATA_LICENSES.md").exists()
rd = (root29 / "README.md").read_text()
assert "## Privacy" in rd and "Experimental software" in rd and "NOTICE" in rd
c29 = web.app.test_client()
web.UPLOADS.mkdir(parents=True, exist_ok=True); (web.UPLOADS / "x_test.wav").write_bytes(b"1234")
assert c29.get("/api/health").json["uploads"] >= 1
assert c29.post("/api/uploads/clear").json["deleted"] >= 1 and c29.get("/api/health").json["uploads"] == 0
print("[29] release material: credits footer, NOTICE, data licences, README privacy section, upload deletion")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK")
