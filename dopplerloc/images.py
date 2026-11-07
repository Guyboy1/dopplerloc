"""Spectrogram PNGs for the manual interfaces (no matplotlib; PIL + numpy)."""
import io
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def _lut():
    stops = [(0, (0, 0, 20)), (0.35, (40, 0, 120)), (0.6, (200, 40, 80)), (0.85, (250, 200, 40)), (1.0, (255, 255, 230))]
    x = np.linspace(0, 1, 256)
    rgb = [np.interp(x, [s[0] for s in stops], [s[1][i] for s in stops]) for i in range(3)]
    return np.stack(rgb, 1).astype(np.uint8)


LUT = _lut()


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                   # very old Pillow
        return ImageFont.load_default()


def _ticks(lo, hi, target=8):
    """'Nice' tick values between lo and hi."""
    span = max(hi - lo, 1e-9)
    raw = span / target
    step = next((s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 25, 30, 50, 60, 100, 120, 200, 300, 600, 1200, 1800, 3600) if s >= raw), 3600)
    first = np.ceil(lo / step) * step
    return [round(v, 6) for v in np.arange(first, hi + 1e-9, step)], step


def render(spec, t0, t1, f0, f1, w=1000, h=420, vmin=3.0, vmax=16.0, curves=None, rects=None, smooth=1.2, axes=True):
    """Spectrogram crop as PNG bytes. x = time [t0,t1], y = frequency [f0,f1] (low at the bottom).
    vmin/vmax: dB above the background that map to black / white (raise vmin for a calmer picture).
    smooth: blur in output pixels (0 = off). axes: draw labelled time and pitch axes around the picture.
    Returns (png_bytes, meta). meta.w/h = size of the plot area; meta.ml/mt = its offset inside the PNG; meta.W/H = whole PNG."""
    ti = np.where((spec.t >= t0) & (spec.t <= t1))[0]
    fi = np.where((spec.f >= f0) & (spec.f <= f1))[0]
    if len(ti) < 2 or len(fi) < 2:
        raise ValueError("empty view (check the time/frequency range)")
    sub = spec.S[np.ix_(fi, ti)]
    g = np.clip((sub - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    img = Image.fromarray((g[::-1] * 255).astype(np.uint8)).resize((w, h), Image.BOX)
    if smooth and smooth > 0:
        from scipy.ndimage import gaussian_filter
        img = Image.fromarray(np.clip(gaussian_filter(np.asarray(img, float), smooth) * 1.15, 0, 255).astype(np.uint8))
    rgb = Image.fromarray(LUT[np.asarray(img)], "RGB")
    ta, tb, fa, fb = float(spec.t[ti[0]]), float(spec.t[ti[-1]]), float(spec.f[fi[0]]), float(spec.f[fi[-1]])
    d = ImageDraw.Draw(rgb)
    for (a, b, lo, hi, col) in (rects or []):          # boxes in data units: (t0, t1, f0, f1, colour)
        x0, x1 = (a - ta) / (tb - ta) * (w - 1), (b - ta) / (tb - ta) * (w - 1)
        y0, y1 = (1 - (hi - fa) / (fb - fa)) * (h - 1), (1 - (lo - fa) / (fb - fa)) * (h - 1)
        d.rectangle([x0, y0, x1, y1], outline=tuple(col), width=2)
    if curves:
        for tt, ff, col in curves:
            tt, ff = np.asarray(tt, float), np.asarray(ff, float)
            ok = np.isfinite(ff)
            xy = [((a - ta) / (tb - ta) * (w - 1), (1 - (b - fa) / (fb - fa)) * (h - 1)) for a, b in zip(tt[ok], ff[ok])]
            if len(xy) > 1:
                d.line(xy, fill=tuple(col), width=1)
    ml = mt = 0
    out = rgb
    if axes:
        ml, mt, mr, mb = 62, 10, 12, 44
        out = Image.new("RGB", (w + ml + mr, h + mt + mb), (24, 24, 30))
        out.paste(rgb, (ml, mt))
        dd = ImageDraw.Draw(out)
        small, big = _font(14), _font(15)
        ink = (225, 228, 235)
        dd.rectangle([ml - 1, mt - 1, ml + w, mt + h], outline=(120, 124, 135))
        tv, tstep = _ticks(ta, tb, max(4, min(12, w // 90)))
        for v in tv:
            x = ml + (v - ta) / (tb - ta) * (w - 1)
            dd.line([(x, mt + h), (x, mt + h + 5)], fill=ink)
            lab = f"{v:g}" if tstep < 60 else f"{int(v) // 60}:{int(v) % 60:02d}"
            dd.text((x, mt + h + 8), lab, fill=ink, font=small, anchor="mt")
        fv, _ = _ticks(fa, fb, max(4, min(10, h // 45)))
        for v in fv:
            y = mt + (1 - (v - fa) / (fb - fa)) * (h - 1)
            dd.line([(ml - 5, y), (ml - 1, y)], fill=ink)
            dd.text((ml - 8, y), f"{v:g}", fill=ink, font=small, anchor="rm")
        dd.text((ml + w / 2, mt + h + 27), "time in the recording (seconds)" if tstep < 60 else "time in the recording (minutes:seconds)",
                fill=ink, font=big, anchor="mt")
        lab = Image.new("RGBA", (200, 22), (0, 0, 0, 0))
        ImageDraw.Draw(lab).text((100, 11), "pitch (Hz)", fill=ink + (255,), font=big, anchor="mm")
        lab = lab.rotate(90, expand=True)
        out.paste(lab, (2, int(mt + h / 2 - lab.height / 2)), lab)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), dict(t0=ta, t1=tb, f0=fa, f1=fb, w=w, h=h, ml=ml, mt=mt, W=out.width, H=out.height)


def render_heat(grid, size=340, label=""):
    """Score grid -> PNG (bright = high score). Cross marks the centre (the candidate). Returns (png, meta)."""
    g = np.asarray(grid, float)
    lo, hi = np.nanmin(g), np.nanmax(g)
    n = (np.nan_to_num(g, nan=lo) - lo) / max(hi - lo, 1e-9)
    img = Image.fromarray((n * 255).astype(np.uint8)).resize((size, size), Image.NEAREST)
    rgb = Image.fromarray(LUT[np.asarray(img)], "RGB")
    d = ImageDraw.Draw(rgb)
    c = size // 2
    d.line([(c - 8, c), (c + 8, c)], fill=(0, 255, 255)); d.line([(c, c - 8), (c, c + 8)], fill=(0, 255, 255))
    if label:
        d.text((4, 3), f"{label}  {lo:.1f}..{hi:.1f}", fill=(255, 255, 255))
    buf = io.BytesIO(); rgb.save(buf, "PNG")
    return buf.getvalue(), dict(min=float(lo), max=float(hi))
