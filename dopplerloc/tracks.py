"""ADS-B ingestion: raw state vectors -> 1 Hz ECEF tracks."""
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from .geo import lla_to_ecef

REQUIRED = ["icao24", "time", "lat", "lon", "alt_m"]


@dataclass
class Track:
    id: str
    t: np.ndarray          # (n,) epoch seconds, uniform spacing
    pos: np.ndarray        # (n,3) ECEF metres
    is_jet: bool = False
    lat: np.ndarray = None
    lon: np.ndarray = None
    alt: np.ndarray = None
    _tree: object = field(default=None, repr=False)

    @property
    def tree(self):
        if self._tree is None:
            self._tree = cKDTree(self.pos)
        return self._tree


def from_opensky(df: pd.DataFrame) -> pd.DataFrame:
    """Adapt an OpenSky state_vectors_data4 frame (pyopensky / traffic output)."""
    t = df["time"]
    t = t.astype("int64") / 1e9 if np.issubdtype(t.dtype, np.datetime64) else t.astype(float)
    alt = df["geoaltitude"] if "geoaltitude" in df else df["baroaltitude"]
    if "baroaltitude" in df:
        alt = alt.fillna(df["baroaltitude"])
    out = pd.DataFrame({"icao24": df["icao24"], "time": t, "lat": df["lat"],
                        "lon": df["lon"], "alt_m": alt})
    if "onground" in df:
        out = out[~df["onground"].astype(bool)]
    return out.dropna()


def build_tracks(df, dt=1.0, max_gap=90.0, min_pts=5, smooth=1):
    """Split on gaps, resample to uniform dt, convert to ECEF."""
    from scipy.ndimage import uniform_filter1d
    missing = [c for c in REQUIRED if c not in df]
    if missing:
        raise ValueError(f"tracks missing columns: {missing}")
    jet_col = "is_jet" in df
    tracks = []
    for icao, g in df.groupby("icao24"):
        g = g.sort_values("time").drop_duplicates("time")
        t = g["time"].to_numpy(float)
        cuts = np.where(np.diff(t) > max_gap)[0] + 1
        for k, seg in enumerate(np.split(np.arange(len(t)), cuts)):
            if len(seg) < min_pts:
                continue
            ts = t[seg]
            tt = np.arange(np.ceil(ts[0] / dt) * dt, ts[-1], dt)
            if len(tt) < 5:
                continue
            lat = np.interp(tt, ts, g["lat"].to_numpy(float)[seg])
            lon = np.interp(tt, ts, g["lon"].to_numpy(float)[seg])
            alt = np.interp(tt, ts, g["alt_m"].to_numpy(float)[seg])
            pos = lla_to_ecef(lat, lon, alt)
            if smooth > 1:
                pos = uniform_filter1d(pos, smooth, axis=0, mode="nearest")
            tracks.append(Track(f"{icao}_{k}", tt, pos,
                                bool(g["is_jet"].iloc[0]) if jet_col else False, lat, lon, alt))
    return tracks
