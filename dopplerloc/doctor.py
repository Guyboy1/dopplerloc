"""Checks everything the program needs and says exactly how to fix what is missing.
Run:  python -m dopplerloc.doctor"""
import importlib, os, sys, urllib.request
from . import net
from pathlib import Path

REQUIRED = [("numpy", "numpy"), ("scipy", "scipy"), ("pandas", "pandas"), ("pyarrow", "pyarrow"), ("flask", "flask"),
            ("PIL", "pillow"), ("tzdata", "tzdata"), ("imageio_ffmpeg", "imageio-ffmpeg")]
OPTIONAL = [("rasterio", "rasterio", "only for your own elevation GeoTIFF"),
            ("pyopensky", "pyopensky", "only for the OpenSky data source")]


def line(ok, name, detail="", mark=None):
    print(f"  [{mark or ('OK' if ok else '!!')}] {name}{(' - ' + detail) if detail else ''}")
    return ok


def main():
    bad = 0
    print("dopplerloc setup check\n")
    print("Python")
    bad += not line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "need 3.10 or newer (python.org/downloads)")
    print("\nRequired packages")
    missing = []
    for mod, pip in REQUIRED:
        try:
            importlib.import_module(mod)
            line(True, pip)
        except Exception:
            missing.append(pip); line(False, pip, "missing")
    if missing:
        bad += len(missing)
        print(f"\n  FIX:  python -m pip install {' '.join(missing)}     (or: python -m pip install -r requirements.txt)")
    print("\nAudio/video tools")
    from . import media
    h = media.health()
    if h["ffmpeg"]:
        line(True, "ffmpeg", h["ffmpeg_path"])
    else:
        bad += 1
        line(False, "ffmpeg", "NOT FOUND")
        print("\n" + "\n".join("    " + l for l in (h["message"] or "").splitlines()))
    line(True, "ffprobe", "found" if h["ffprobe"] else "not installed (fine - ffmpeg alone is used)", mark="OK" if h["ffprobe"] else "--")
    line(True, "exiftool", "found (extra metadata)" if h["exiftool"] else "not installed (optional; finds more metadata in photos/videos)", mark="OK" if h["exiftool"] else "--")
    print("\nOptional")
    for mod, pip, why in OPTIONAL:
        try:
            importlib.import_module(mod); line(True, pip)
        except Exception:
            line(True, pip, f"not installed ({why}); install with: python -m pip install {pip}", mark="--")
    try:
        from pyopensky.config import opensky_config_dir
        conf = Path(opensky_config_dir) / "settings.conf"
        have = conf.exists() and any(l.split("=")[0].strip() == "username" and l.split("=", 1)[1].strip()
                                     for l in conf.read_text().splitlines() if "=" in l)
        line(True, "OpenSky settings", f"{conf}  ({'username set' if have else 'no username saved yet - you can type it in the page instead'})")
    except Exception:
        pass
    print("\nInternet (needed for maps, terrain tiles, adsb.lol / OpenSky downloads)")
    for name, url in (("GitHub", "https://github.com"), ("OpenStreetMap", "https://tile.openstreetmap.org/0/0/0.png")):
        try:
            net.urlopen(urllib.request.Request(url, method="HEAD" if "github" in url else "GET", headers={"User-Agent": "dopplerloc"}), timeout=8)
            line(True, name)
        except Exception as e:
            line(False, name, "certificate problem - see SETUP_GUIDE.md, section 'Certificate error'" if isinstance(e, net.NetError)
                 else f"cannot reach ({type(e).__name__}); needed for online features only")
    from . import places
    print(f"\nCountries in the picker: {len(places.countries())} (verified with the pipeline self-test)")
    print("\nAll good - start with:  python -m dopplerloc.web" if not bad else f"\n{bad} problem(s) above. Fix them, then run this check again.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
