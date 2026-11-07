"""Any audio/video file -> mono audio, plus every location / time clue we can find in its metadata.

ffmpeg: uses the one on PATH, else the copy bundled by the `imageio-ffmpeg` pip package (so a plain
`pip install -r requirements.txt` is enough). ffprobe is optional (we fall back to `ffmpeg -f ffmetadata`).
exiftool: optional; when installed it is used too and finds much more (EXIF GPS, IPTC/XMP place names, GPS clock).
"""
import json, os, re, shutil, subprocess, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

FFMPEG_HELP = ("ffmpeg was not found. Fix (pick one):\n"
               "  1) pip install imageio-ffmpeg        (easiest; works on Windows/macOS/Linux)\n"
               "  2) Windows: winget install Gyan.FFmpeg   |  macOS: brew install ffmpeg   |  Linux: sudo apt install ffmpeg\n"
               "then restart the program. Full steps: SETUP_GUIDE.md")


# ----------------------------------------------------------------------------- finding the programs
def _bundled_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_exe():
    if not os.environ.get("DOPPLERLOC_FORCE_BUNDLED"):
        p = shutil.which("ffmpeg")
        if p:
            return p
    b = _bundled_ffmpeg()
    if b:
        return b
    raise RuntimeError(FFMPEG_HELP)


def ffprobe_exe():
    return None if os.environ.get("DOPPLERLOC_FORCE_BUNDLED") else shutil.which("ffprobe")


def exiftool_exe():
    return None if os.environ.get("DOPPLERLOC_NO_EXIFTOOL") else shutil.which("exiftool")


def health():
    """What the web page shows at the top if something is missing."""
    try:
        ff, err = ffmpeg_exe(), None
    except RuntimeError as e:
        ff, err = None, str(e)
    return dict(ffmpeg=bool(ff), ffmpeg_path=ff, ffprobe=bool(ffprobe_exe()), exiftool=bool(exiftool_exe()), message=err)


# ----------------------------------------------------------------------------- probing
def _unescape(v):
    return re.sub(r"\\(.)", r"\1", v)


def _probe_ffmpeg_only(path):
    """No ffprobe: read tags with `ffmpeg -i f -f ffmetadata -` and duration/streams from the banner."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "ffmetadata", "-"], capture_output=True, text=True,
                       errors="replace")
    if r.returncode != 0 and "Duration" not in r.stderr:
        raise RuntimeError("ffmpeg could not read this file: " + r.stderr.strip()[-300:])
    tags = {}
    for line in r.stdout.splitlines():
        if line.startswith(";") or line.startswith("["):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            tags.setdefault(_unescape(k).lower(), _unescape(v))
    for line in r.stderr.splitlines():                  # the banner also lists tags ffmetadata leaves out (creation_time ...)
        mm = re.match(r"^\s+([A-Za-z0-9_.\-©]+)\s+:\s(.+)$", line)
        if mm:
            tags.setdefault(mm.group(1).lower(), mm.group(2).strip())
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return dict(duration=dur, has_audio=bool(re.search(r"Stream #\S+.*Audio:", r.stderr)),
                has_video=bool(re.search(r"Stream #\S+.*Video:", r.stderr)) and "attached pic" not in r.stderr, tags=tags)


def probe(path):
    """-> dict(duration, has_audio, has_video, tags). tags: format + stream tags, lower-cased keys."""
    ffmpeg_exe()                                       # raises the friendly message if nothing is installed
    fp = ffprobe_exe()
    if not fp:
        return _probe_ffmpeg_only(path)
    r = subprocess.run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffprobe could not read this file: " + r.stderr.strip()[:300])
    j = json.loads(r.stdout)
    tags = {}
    for src in [j.get("format", {})] + j.get("streams", []):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), str(v))
    kinds = {s.get("codec_type") for s in j.get("streams", []) if not (s.get("disposition") or {}).get("attached_pic")}
    return dict(duration=float(j.get("format", {}).get("duration") or 0), has_audio="audio" in kinds,
                has_video="video" in kinds, tags=tags)


def exiftool_tags(path):
    """Optional richer metadata. Keys come back as 'Group:Tag' (lower-cased)."""
    et = exiftool_exe()
    if not et:
        return {}
    try:
        r = subprocess.run([et, "-j", "-n", "-a", "-G1", "-api", "QuickTimeUTC=1", str(path)], capture_output=True, text=True,
                           errors="replace", timeout=60)
        d = json.loads(r.stdout)[0]
    except Exception:
        return {}
    return {k.lower(): v for k, v in d.items() if not isinstance(v, (list, dict))}


# ----------------------------------------------------------------------------- location clues
_ISO6709 = re.compile(r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")
_DEC_PAIR = re.compile(r"(-?\d{1,3}\.\d{3,})\s*[,; ]\s*(-?\d{1,3}\.\d{3,})")
_DMS = re.compile(r"(\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([NS])[^0-9A-Za-z]*(\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([EW])", re.I)
GPS_KEY = re.compile(r"gps|location|geo|coordinate|latitude|longitude|position|xyz", re.I)


def parse_iso6709(s):
    m = _ISO6709.match(s or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def _coords_from_text(s, strict=False):
    """ISO 6709 (+27.9506-082.4572/), 'lat, lon' decimals, or DMS 27 57 2.2 N 82 27 25 W.
    strict=True (free-text tags): needs >=4 decimals or explicit N/S/E/W so ordinary numbers are not mistaken."""
    s = str(s)
    c = None if strict else parse_iso6709(s)
    if c:
        return c
    m = _DEC_PAIR.search(s)
    if m and strict and (len(m.group(1).split(".")[1]) < 4 or len(m.group(2).split(".")[1]) < 4):
        m = None
    if m:
        la, lo = float(m.group(1)), float(m.group(2))
        if abs(la) <= 90 and abs(lo) <= 180:
            return (la, lo)
    m = _DMS.search(s)
    if m:
        g = m.groups()
        la = (int(g[0]) + int(g[1]) / 60 + float(g[2]) / 3600) * (1 if g[3].upper() == "N" else -1)
        lo = (int(g[4]) + int(g[5]) / 60 + float(g[6]) / 3600) * (1 if g[7].upper() == "E" else -1)
        if abs(la) <= 90 and abs(lo) <= 180:
            return (la, lo)
    return None


def find_gps(tags):
    """-> (lat, lon, source_key) or None. Tries well-known keys, exiftool composites, then any plausible tag."""
    et = tags
    for klat, klon in (("composite:gpslatitude", "composite:gpslongitude"), ("exif:gpslatitude", "exif:gpslongitude"),
                       ("gps:gpslatitude", "gps:gpslongitude"), ("quicktime:gpslatitude", "quicktime:gpslongitude")):
        if isinstance(et.get(klat), (int, float)) and isinstance(et.get(klon), (int, float)) and (et[klat] or et[klon]):
            return float(et[klat]), float(et[klon]), klat.split(":")[0] + " GPS"
    for k in ("com.apple.quicktime.location.iso6709", "location", "location-eng", "xyz", "©xyz", "com.android.location",
              "keys:gpscoordinates", "quicktime:gpscoordinates", "userdata:gpscoordinates"):
        if k in tags and (c := _coords_from_text(tags[k])):
            return c[0], c[1], k
    lat = lon = None
    for k, v in tags.items():
        kl = k.lower()
        if "latitude" in kl and "ref" not in kl:
            lat = _num(v, lat)
        if "longitude" in kl and "ref" not in kl:
            lon = _num(v, lon)
    if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180 and (lat or lon):
        return lat, lon, "latitude/longitude tags"
    for k, v in tags.items():
        if GPS_KEY.search(k) and isinstance(v, str) and (c := _coords_from_text(v)):
            return c[0], c[1], k
    for k, v in tags.items():                                      # any other text tag (comment, description, title ...)
        if isinstance(v, str) and len(v) < 500 and (c := _coords_from_text(v, strict=True)):
            return c[0], c[1], k + " (text)"
    return None


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def place_hints(tags):
    """General location names (country / state / city) if the file carries any."""
    def pick(*names):
        for k, v in tags.items():
            kl = k.lower().split(":")[-1]
            if kl in names and isinstance(v, str) and v.strip():
                return v.strip()
    h = dict(country=pick("country", "country-primarylocationname", "countryname", "location_country", "countrycode"),
             state=pick("province-state", "state", "province", "region", "location_state"),
             city=pick("city", "location_city", "sublocation", "location_name", "com.apple.quicktime.location.name"))
    return {k: v for k, v in h.items() if v}


# ----------------------------------------------------------------------------- time clues
_DT = re.compile(r"(\d{4})[-:/.](\d{2})[-:/.](\d{2})[T _]+(\d{2})[:.](\d{2})[:.](\d{2})(?:[.,](\d+))?\s*(Z|UTC|[+-]\d{2}:?\d{2})?", re.I)
_FNAME = re.compile(r"(20\d{2})[-_.]?([01]\d)[-_.]?([0-3]\d)[ _T.-]*(?:at )?([0-2]\d)[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)")


def parse_dt(s, utc_hint=False):
    """-> (aware datetime | None, naive datetime | None). Aware only when the text says Z/UTC/offset (or utc_hint)."""
    m = _DT.search(str(s))
    if not m:
        return None, None
    y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    try:
        base = datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None, None
    z = m.group(8)
    if z:
        if z.upper() in ("Z", "UTC"):
            return base.replace(tzinfo=timezone.utc), None
        sign = 1 if z[0] == "+" else -1
        zz = z[1:].replace(":", "")
        return base.replace(tzinfo=timezone(sign * timedelta(hours=int(zz[:2]), minutes=int(zz[2:])))), None
    if utc_hint or str(s).strip().upper().startswith("UTC"):
        return base.replace(tzinfo=timezone.utc), None
    return None, base


def find_time(tags, filename=""):
    """Best capture-time clue -> dict(time_utc?, time_local?, utc_offset_min?, time_source, time_confidence, time_tolerance_s)."""
    cands = []        # (priority, aware, naive, source, confidence, tolerance)

    def add(pri, key, label, conf, tol, utc_hint=False):
        if key in tags:
            a, n = parse_dt(tags[key], utc_hint)
            if a or n:
                cands.append((pri, a, n, label, conf, tol))

    add(0, "composite:gpsdatetime", "GPS satellite clock", "high", 2.0, True)
    add(1, "com.apple.quicktime.creationdate", "Apple 'creation date' (device clock, with local offset)", "high", 2.0)
    add(1, "keys:creationdate", "Apple 'creation date' (device clock, with local offset)", "high", 2.0)
    add(2, "exif:datetimeoriginal", "EXIF original date/time", "medium", 5.0)
    add(3, "quicktime:createdate", "QuickTime create date", "medium", 30.0, True)
    add(3, "creation_time", "container creation_time", "medium", 30.0, True)
    add(4, "date_time_original", "date_time_original tag", "medium", 30.0)
    add(4, "encoded_date", "encoded_date tag", "medium", 30.0, True)
    add(4, "tagged_date", "tagged_date tag", "medium", 30.0, True)
    add(4, "date_recorded", "date_recorded tag", "medium", 30.0)
    add(5, "date", "date tag", "low", 60.0)
    add(5, "creation_date", "creation date tag", "low", 60.0)
    if "origination_date" in tags and "origination_time" in tags:       # broadcast-WAV (field recorders)
        a, n = parse_dt(f"{tags['origination_date']} {tags['origination_time']}")
        if a or n:
            cands.append((2, a, n, "WAV broadcast origination time", "medium", 5.0))
    fm = _FNAME.search(Path(filename).stem) if filename else None
    if fm:
        try:
            cands.append((9, None, datetime(*(int(x) for x in fm.groups())), "timestamp in the file name", "low", 60.0))
        except ValueError:
            pass
    if not cands:
        return {}
    lo, hi = datetime(2000, 1, 1), datetime(2100, 1, 1)           # QuickTime's "zero" date is 1904: not a real clue
    cands = [c for c in cands if lo <= ((c[1].replace(tzinfo=None) if c[1] else c[2])) < hi]
    if not cands:
        return {}
    cands.sort(key=lambda c: c[0])
    pri, a, n, label, conf, tol = cands[0]
    out = dict(time_source=label, time_confidence=conf, time_tolerance_s=tol)
    if a:
        out["time_utc"] = a.timestamp()
        out["utc_offset_min"] = a.utcoffset().total_seconds() / 60 if a.utcoffset() is not None else 0.0
        out["time_has_offset"] = "offset" in label or a.utcoffset() != timedelta(0)
    else:
        out["time_local"] = n.strftime("%Y-%m-%d %H:%M:%S")          # wall-clock time, zone unknown
    return out


# ----------------------------------------------------------------------------- everything together
def metadata(tags, filename="", duration=0.0):
    """Collect location + time clues from a tag dict (ffprobe tags and/or exiftool tags)."""
    out = {}
    g = find_gps(tags)
    if g:
        out.update(lat=g[0], lon=g[1], gps_source=g[2])
        out["gps_decimals"] = max(len(str(x).split(".")[-1]) if "." in str(x) else 0 for x in g[:2])
    hints = place_hints(tags)
    if hints:
        out["place_hints"] = hints
    out.update(find_time(tags, filename))
    dev = " ".join(x for x in (tags.get("com.apple.quicktime.make"), tags.get("com.apple.quicktime.model"),
                               tags.get("com.android.manufacturer"), tags.get("com.android.model"),
                               tags.get("exif:make"), tags.get("exif:model")) if x)
    if dev:
        out["device"] = dev
    return out


def inspect(path):
    """probe + metadata (ffprobe/ffmpeg tags, exiftool if installed, file name). Used by the web upload."""
    info = probe(path)
    tags = dict(info["tags"])
    for k, v in exiftool_tags(path).items():
        tags.setdefault(k, v)
    info["metadata"] = metadata(tags, Path(path).name, info["duration"])
    info["metadata_tool"] = "ffmpeg" + ("/ffprobe" if ffprobe_exe() else "") + (" + exiftool" if exiftool_exe() else "")
    return info


def extract_audio(path, fs=2000, out_dir=None):
    """Convert anything ffmpeg can read to a mono 32-bit-float WAV at fs Hz (float keeps very faint curves intact)."""
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError("this file has no audio track")
    out = Path(out_dir or tempfile.mkdtemp(prefix="dopplerloc_")) / (Path(path).stem + f"_{fs}.wav")
    r = subprocess.run([ffmpeg_exe(), "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(fs), "-c:a", "pcm_f32le", str(out)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.strip()[-300:])
    return str(out), info
