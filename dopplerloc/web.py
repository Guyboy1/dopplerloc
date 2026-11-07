"""Local web interface:  python -m dopplerloc.web   ->  http://127.0.0.1:5000"""
import json, sys, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, send_from_directory, Response

from . import media, places, images, providers
from .audio import make_spec_view
from .jobs import Job, run
from .typedb import CACHE

WARNING = ("Locating a recording can expose where a person was. Only analyse recordings you made, or that you have "
           "explicit permission to analyse. Do not use this to find, follow, or identify people without their consent.")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3
UPLOADS = CACHE / "uploads"
JOBS, FILES = {}, {}


@app.get("/")
def index():
    return send_from_directory(Path(__file__).parent / "static", "index.html")


# ------------------------------------------------------------ places
@app.get("/api/places/countries")
def api_countries():
    return jsonify(places.countries())


@app.get("/api/coverage")
def api_coverage():
    """Flight-data coverage hint for a country and (optionally) the current search box S,W,N,E."""
    from .coverage import assess
    try:
        bb = [float(request.args[k]) for k in ("s", "w", "n", "e")]
    except (KeyError, ValueError):
        bb = None
    return jsonify(assess(request.args.get("country"), bb))


@app.get("/api/places/states")
def api_states():
    return jsonify(places.states(request.args["country"]))


@app.get("/api/places/cities")
def api_cities():
    sid = request.args.get("state")
    return jsonify(places.cities(request.args["country"], int(sid) if sid else None))


@app.get("/api/places/resolve")
def api_resolve():
    try:
        r = places.resolve(request.args["country"], request.args.get("state") or None, request.args.get("city") or None)
        return jsonify(r)
    except KeyError as e:
        return jsonify(error=str(e)), 404


# ------------------------------------------------------------ upload + metadata prefill
def prefill(meta):
    """Turn metadata clues into values for the page's boxes."""
    out = {}
    tz = None
    if "lat" in meta:
        n = places.nearest(meta["lat"], meta["lon"])
        tz = n["tz"]
        out.update(country=n["country"], state_id=n["state_id"], city=n["city"], tz=tz, nearest_km=round(n["km"], 1),
                   gps=[meta["lat"], meta["lon"]],
                   bbox=[meta["lat"] - 0.1, meta["lon"] - 0.1, meta["lat"] + 0.1, meta["lon"] + 0.1])
    elif meta.get("place_hints"):                      # only names (country / state / city) in the file
        h = places.match_hint(**{k: meta["place_hints"].get(k) for k in ("country", "state", "city")})
        if h.get("country"):
            out.update(country=h["country"], state_id=h.get("state_id"), city=h.get("city"), from_name=True)
    off = meta.get("utc_offset_min")
    if "time_utc" in meta:
        if tz is None and off is not None and off % 60 == 0:
            tz = f"Etc/GMT{'-' if off >= 0 else '+'}{int(abs(off) // 60)}" if off else "UTC"
        if tz:
            loc = datetime.fromtimestamp(meta["time_utc"], ZoneInfo(tz))
            out.update(tz=tz, year=loc.year, month=loc.month, day=loc.day, exact=loc.strftime("%H:%M:%S"))
        else:
            out["time_utc"] = meta["time_utc"]
    elif "time_local" in meta:                         # wall-clock time without a zone: the page uses the chosen time zone
        d, t = meta["time_local"].split(" ")
        y, mo, da = (int(x) for x in d.split("-"))
        out.update(year=y, month=mo, day=da, exact=t)
    if "exact" in out or "time_utc" in out:
        out.update(time_tolerance_s=meta.get("time_tolerance_s", 5.0), time_source=meta.get("time_source"),
                   time_confidence=meta.get("time_confidence"))
    if tz:
        out["tz"] = tz
    return out


@app.get("/api/health")
def api_health():
    h = media.health()
    h["warning"] = WARNING
    h["free_gb"] = round(providers.free_gb(), 1)
    h["cache_gb"] = round(providers.cache_bytes() / 1e9, 2)
    h["uploads"] = sum(1 for f in UPLOADS.glob("*") if f.is_file()) if UPLOADS.exists() else 0
    return jsonify(h)


@app.post("/api/uploads/clear")
def api_uploads_clear():
    """Delete the recordings you uploaded (they are otherwise kept in the cache folder)."""
    n = 0
    if UPLOADS.exists():
        for f in UPLOADS.glob("*"):
            try:
                f.unlink(); n += 1
            except OSError:
                pass
    FILES.clear()
    return jsonify(deleted=n)


@app.post("/api/cache/clear")
def api_cache_clear():
    return jsonify(freed_gb=round(providers.clear_cache() / 1e9, 2), free_gb=round(providers.free_gb(), 1))


@app.post("/api/upload")
def api_upload():
    f = request.files["file"]
    UPLOADS.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex[:10]
    path = UPLOADS / f"{fid}_{Path(f.filename).name}"
    f.save(path)
    try:
        info = media.inspect(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify(error=str(e)), 400
    if not info["has_audio"]:
        return jsonify(error="That file has no audio track."), 400
    meta = info["metadata"]
    FILES[fid] = str(path)
    return jsonify(file_id=fid, name=f.filename, duration=info["duration"], has_video=info["has_video"],
                   metadata=meta, prefill=prefill(meta), tool=info["metadata_tool"])


# ------------------------------------------------------------ jobs
@app.post("/api/start")
def api_start():
    b = request.get_json()
    if not b.get("ack"):
        return jsonify(error="Please tick the permission / responsible-use box."), 400
    if b.get("file_id") not in FILES:
        return jsonify(error="Upload the audio/video file first."), 400
    try:
        tf = dict(year=int(b["year"]), month=int(b["month"]), tz=b["tz"], cuts=bool(b.get("cuts")),
                  span_days=int(b.get("span_days") or 1), tolerance_s=float(b.get("tolerance_s") or 5.0))
        if b.get("day"):
            tf["day"] = int(b["day"])
        for k in ("start", "end", "exact"):
            if b.get(k):
                tf[k] = b[k]
    except (KeyError, ValueError) as e:
        return jsonify(error=f"check the time boxes: {e}"), 400
    opts = dict(ack=True, audio_path=FILES[b["file_id"]], bbox=b["bbox"], timeframe=tf, source=b.get("source", "file"),
                flight_file=b.get("flight_file") or None, dem=b.get("dem") or "flat", temp_c=b.get("temp_c"),
                workers=b.get("workers"), always_manual=bool(b.get("always_manual")), params=b.get("params"),
                metadata_end=bool(b.get("metadata_end")), country=b.get("country") or None,
                   low_storage=bool(b.get("low_storage")), stop_early=b.get("stop_early", True) is not False, opensky_user=b.get("opensky_user"), opensky_pass=b.get("opensky_pass"))
    job = Job()
    JOBS[job.id] = job
    threading.Thread(target=run, args=(job, opts), daemon=True).start()
    return jsonify(job_id=job.id)


@app.get("/api/job/<jid>")
def api_job(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="unknown job"), 404
    return jsonify(id=j.id, status=j.status, percent=j.percent, steps=j.steps, logs=j.logs[-40:], error=j.error,
                   manual=j.manual, result=j.result, **j.timing())


@app.post("/api/job/<jid>/manual")
def api_manual(jid):
    JOBS[jid].answer(request.get_json())
    return jsonify(ok=True)


@app.post("/api/job/<jid>/cancel")
def api_cancel(jid):
    JOBS[jid].cancel()
    return jsonify(ok=True)


def _png(data, meta):
    return Response(data, mimetype="image/png", headers={"X-Meta": json.dumps(meta), "Access-Control-Expose-Headers": "X-Meta"})


@app.get("/api/job/<jid>/heat/<name>")
def api_heat(jid, name):
    j = JOBS[jid]
    if name not in j.heat:
        return jsonify(error="no such heat map"), 404
    return _png(*j.heat[name])


RES = {"low": 2048, "med": 4096, "high": 8192, "max": 16384}


@app.get("/api/job/<jid>/img")
def api_img(jid):
    j, a = JOBS[jid], request.args
    spec = j.spec
    if a.get("res") in RES and RES[a["res"]] != 4096 and j.audio is not None:      # zoom with more resolution
        spec = make_spec_view(*j.audio[:1], j.audio[1], float(a["t0"]), float(a["t1"]), RES[a["res"]])
    try:
        data, meta = images.render(spec, float(a["t0"]), float(a["t1"]), float(a["f0"]), float(a["f1"]),
                                   int(a.get("w", 1000)), int(a.get("h", 420)), vmin=float(a.get("vmin", 3)), vmax=float(a.get("vmax", 16)),
                                   smooth=float(a.get("smooth", 1.2)))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return _png(data, meta)


@app.get("/api/job/<jid>/preview")
def api_preview(jid):
    """Small live thumbnail of the whole recording with the events found so far boxed."""
    j = JOBS[jid]
    if j.spec is None:
        return jsonify(error="not ready"), 404
    rects = [(e["win"][0], e["win"][1], e["band"][0], e["band"][1], e["color"]) for e in j.preview]
    fmax = min(600.0, float(j.spec.f[-1]))
    data, meta = images.render(j.spec, 0, float(j.spec.t[-1]), 20, fmax, 700, 110, rects=rects, axes=False)
    return _png(data, meta)


@app.get("/api/job/<jid>/overlay/<int:k>/<int:e>")
def api_overlay(jid, k, e):
    j = JOBS[jid]
    if e >= len(j.overlays[k]):
        return jsonify(error="no second curve"), 404
    c = j.overlays[k][e]
    f_all = [v for fl in c["f"] for v in fl if v == v]
    lo, hi = max(min(f_all) - 20, 0), max(f_all) + 20
    curves = [(c["t"], fl, (60, 255, 90)) for fl in c["f"]]
    data, meta = images.render(j.spec, c["win"][0], c["win"][1], lo, hi, 700, 300, curves=curves, vmin=2, vmax=16, smooth=0.8)
    return _png(data, meta)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print("\n" + "=" * 72 + "\nRESPONSIBLE USE\n" + WARNING + "\n" + "=" * 72)
    print(f"\nOpen http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
