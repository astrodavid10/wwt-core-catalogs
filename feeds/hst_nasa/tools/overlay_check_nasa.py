"""Visual check of a sidecar WCS: DSS2 (IRSA HiPS) sampled on the image's own grid, shown
next to the image and as a blink-style overlay (image in red/green, DSS in blue).

usage (base python): python tools/overlay_check_nasa.py <feed_dir> <out_dir> <cache_dir> id [id ...]
(adapted from feeds/chandra/_assessment/scripts/overlay_check.py: images come from _png/<id>.png)
"""
import os, sys, json
import numpy as np
from PIL import Image
from astropy.io import fits
from astropy.wcs import WCS
from astropy_healpix import HEALPix
import astropy.units as u

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'chandra', '_assessment', 'scripts'))
import hips_sample

Image.MAX_IMAGE_PIXELS = None
feed, out, cache = sys.argv[1:4]
os.makedirs(out, exist_ok=True)
URL, ORDER = 'https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/red', 9

for uid in sys.argv[4:]:
    h = fits.getheader(os.path.join(feed, 'wcs', uid + '.fits'))
    w = WCS(h)
    W, H = h['IMAGEW'], h['IMAGEH']
    img = Image.open(os.path.join(feed, '_png', uid + '.png')).convert('L')
    n = 700
    z = n / max(W, H)
    small = np.asarray(img.resize((int(W * z), int(H * z)), Image.LANCZOS), np.float32)
    hh, ww = small.shape
    yy, xx = np.mgrid[0:hh, 0:ww]
    # small pixel (x, y top-down) -> full-res FITS pixel (x/z, H-1-y/z)
    sky = w.pixel_to_world(xx.ravel() / z, (H - 1) - yy.ravel() / z)
    hp = HEALPix(nside=2 ** (ORDER + 9), order='nested')
    idx = hp.lonlat_to_healpix(sky.ra, sky.dec).astype(np.int64)
    tile = idx >> 18; sub = idx & ((1 << 18) - 1)
    c = hips_sample._compress(sub); r = hips_sample._compress(sub >> 1)  # layout (1, 0, False, False)
    col, row = r, c
    dss = np.zeros(hh * ww, np.float32)
    for t in np.unique(tile):
        tim = hips_sample._tile(URL, ORDER, int(t), cache, 'jpg')
        if tim is None:
            continue
        m = tile == t
        dss[m] = tim[row[m], col[m]].mean(axis=1)
    dss = dss.reshape(hh, ww)
    norm = lambda a: np.clip((a - np.percentile(a, 1)) / (np.percentile(a, 99.5) - np.percentile(a, 1) + 1e-6), 0, 1)
    a, b = norm(small), norm(dss)
    ov = np.dstack([a, a * 0.6, b]) * 255
    panel = np.concatenate([np.dstack([a] * 3) * 255, np.dstack([b] * 3) * 255, ov], axis=1).astype(np.uint8)
    Image.fromarray(panel).save(os.path.join(out, uid + '_check.jpg'), quality=85)
    # independent score: share of the image's point sources with a DSS star within 2 px, vs chance
    import register
    pa = register.detect_points((a * 255).astype(np.uint8), 60)
    pb = register.detect_points((b * 255).astype(np.uint8), 200)
    frac = chance = float('nan')
    if len(pa) >= 5 and len(pb) >= 5 and b.std() > 0.01:
        d2 = ((pa[:, None, :] - pb[None, :, :]) ** 2).sum(-1).min(1)
        frac = float((d2 < 4).mean())
        chance = min(1.0, len(pb) * np.pi * 4 / (hh * ww))
    print(f'{uid},{h.get("WCSSRC")},{h.get("RMSARCS")},{frac:.2f},{chance:.3f}', flush=True)
