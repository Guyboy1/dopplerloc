"""Aircraft type -> jet / prop. Uses ICAO Doc 8643 engine types (bundled) and, when a source
lacks type codes (e.g. OpenSky), the tar1090-db hex->type table (downloaded once, cached)."""
import csv, gzip, io, os, urllib.request
from functools import lru_cache
from pathlib import Path

CACHE = Path(os.environ.get("DOPPLERLOC_CACHE", Path.home() / ".dopplerloc"))
HEX_DB_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"


@lru_cache(maxsize=1)
def engine_by_type():
    out = {}
    with open(Path(__file__).parent / "data" / "typecodes.csv", encoding="utf8", newline="") as f:
        for r in csv.DictReader(f):
            out[r["typecode"]] = r["engine"]
    return out


def is_jet_type(typecode):
    """True only for KNOWN jets. Unknown types stay 'maybe prop' (as in the video)."""
    return bool(typecode) and engine_by_type().get(str(typecode).upper()) == "J"


def hex_to_type(log=print):
    """{icao24 hex (lower): typecode}. Downloads ~8 MB once."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "aircraft.csv.gz"
    if not p.exists():
        log("downloading aircraft type database (tar1090-db) ...")
        from . import net
        net.retrieve(HEX_DB_URL, p)
    out = {}
    with gzip.open(p, "rt", encoding="utf8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) > 2 and parts[2]:
                out[parts[0].lower()] = parts[2]
    return out


def flag_jets(df, log=print):
    """Add is_jet to a tracks DataFrame, using its `typecode` column or the hex lookup."""
    df = df.copy()
    if "typecode" not in df:
        try:
            m = hex_to_type(log)
            df["typecode"] = df["icao24"].str.lower().map(m)
        except Exception as e:                                   # offline -> keep everything as 'maybe prop'
            log(f"warning: could not load aircraft type database ({e}); no jets will be filtered")
            df["typecode"] = None
    df["is_jet"] = df["typecode"].map(is_jet_type)
    return df
