"""Coordinates, ground grid, DEM lookup."""
import numpy as np

_A = 6378137.0
_E2 = 6.69437999014e-3
M_PER_DEG = 111_320.0


def lla_to_ecef(lat, lon, h):
    lat = np.radians(np.asarray(lat, float))
    lon = np.radians(np.asarray(lon, float))
    h = np.asarray(h, float)
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    return np.stack([(n + h) * np.cos(lat) * np.cos(lon),
                     (n + h) * np.cos(lat) * np.sin(lon),
                     (n * (1 - _E2) + h) * np.sin(lat)], axis=-1)


def offset_latlon(lat, lon, dn_m, de_m):
    """Shift a point by metres north / east (fine for offsets of a few km)."""
    return (lat + dn_m / M_PER_DEG,
            lon + de_m / (M_PER_DEG * np.cos(np.radians(lat))))


def make_grid(bbox, step_m):
    """bbox = (min_lat, min_lon, max_lat, max_lon) -> flat lat, lon arrays."""
    min_lat, min_lon, max_lat, max_lon = bbox
    mid = np.radians((min_lat + max_lat) / 2)
    lats = np.arange(min_lat, max_lat, step_m / M_PER_DEG)
    lons = np.arange(min_lon, max_lon, step_m / (M_PER_DEG * np.cos(mid)))
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return la.ravel(), lo.ravel()


class DEM:
    """Ground elevation in metres. DEM() is flat 0 m (used in tests)."""

    def __init__(self, arr=None, transform=None, const=0.0):
        self.arr, self.tr, self.const = arr, transform, const
        self.has_data = arr is not None

    @classmethod
    def from_geotiff(cls, path, bbox, pad=0.1):
        """Load only the ROI window of a GeoTIFF (SRTM / USGS 3DEP etc.)."""
        import rasterio
        from rasterio.windows import from_bounds
        min_lat, min_lon, max_lat, max_lon = bbox
        with rasterio.open(path) as src:
            win = from_bounds(min_lon - pad, min_lat - pad,
                              max_lon + pad, max_lat + pad, src.transform)
            arr = src.read(1, window=win, boundless=True, fill_value=0).astype(float)
            tr = src.window_transform(win)
        return cls(arr, tr)

    def __call__(self, lat, lon):
        lat = np.asarray(lat, float)
        if self.arr is None:
            return np.full(lat.shape, self.const)
        col, row = ~self.tr * (np.asarray(lon, float), lat)
        r = np.clip(np.floor(row).astype(int), 0, self.arr.shape[0] - 1)
        c = np.clip(np.floor(col).astype(int), 0, self.arr.shape[1] - 1)
        return self.arr[r, c]


class TerrainTilesDEM:
    """Global elevation from the public AWS 'Terrain Tiles' dataset (Terrarium PNGs), fetched lazily and
    cached on disk, so a country-wide search only downloads tiles near flight paths.
    elevation_m = (R*256 + G + B/256) - 32768. Zoom 10 ~ 150 m/pixel at the equator."""
    URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

    def __init__(self, zoom=10, cache_dir=None, offline=False, log=print):
        import os
        from pathlib import Path
        self.z, self.offline, self.log = zoom, offline, log
        self.dir = Path(cache_dir or os.environ.get("DOPPLERLOC_CACHE", Path.home() / ".dopplerloc")) / "terrain" / str(zoom)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._tiles, self.failed, self.nfail = {}, set(), 0

    def _tile(self, x, y):
        key = (x, y)
        if key in self._tiles:
            return self._tiles[key]
        arr = None
        p = self.dir / f"{x}_{y}.png"
        if not p.exists() and not self.offline and key not in self.failed:
            try:
                from . import net
                with net.urlopen(self.URL.format(z=self.z, x=x, y=y), timeout=20) as r:
                    p.write_bytes(r.read())
            except Exception as e:
                self.failed.add(key)
                self.nfail += 1
                if self.nfail <= 3:
                    self.log(f"warning: terrain tile {x},{y} unavailable ({e}); using 0 m there"
                             + (" (further tile warnings hidden)" if self.nfail == 3 else ""))
        if p.exists():
            from PIL import Image
            im = np.asarray(Image.open(p).convert("RGB"), dtype=np.float32)
            arr = im[..., 0] * 256 + im[..., 1] + im[..., 2] / 256 - 32768
        self._tiles[key] = arr
        return arr

    def __call__(self, lat, lon):
        lat = np.asarray(lat, float); lon = np.asarray(lon, float)
        n = 2 ** self.z
        fx = (lon + 180.0) / 360.0 * n
        fy = (1 - np.arcsinh(np.tan(np.radians(np.clip(lat, -85, 85)))) / np.pi) / 2 * n
        tx, ty = np.floor(fx).astype(int), np.floor(fy).astype(int)
        px = np.clip(((fx - tx) * 256).astype(int), 0, 255)
        py = np.clip(((fy - ty) * 256).astype(int), 0, 255)
        out = np.zeros(lat.shape)
        key = tx.astype(np.int64) * 10_000_000 + ty
        for k in np.unique(key):
            m = key == k
            arr = self._tile(int(k // 10_000_000), int(k % 10_000_000))
            if arr is not None:
                out[m] = arr[py[m], px[m]]
        return out

    has_data = True

    def __getstate__(self):
        d = dict(self.__dict__); d["log"] = print; return d
