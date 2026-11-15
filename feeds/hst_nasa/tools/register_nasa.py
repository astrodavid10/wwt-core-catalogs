"""Register the NASA-only Hubble images (no AVM) against survey images.

    python tools/register_nasa.py [id ...]     (base anaconda python: needs cv2, astropy_healpix)

Reuses feeds/chandra/_assessment/scripts/register.py (SIFT/point-pattern matching,
TAN fit, sanity checks). Seeds come from candidates.json (SIMBAD position of the
object named on the page). Hubble's pixel scale isn't stated, so it tries field
widths of ~3.4' (one ACS/WFC3 frame), 10' (mosaic) and 1' (close crop). The
search window is generous (at least 8') because the named object is not always
at the image centre. Accepted fits go to wcs/<id>.fits (FITS pixel convention,
IMAGEW/IMAGEH = image size); every attempt is logged to wcs/_register_log.csv.
"""

import csv
import json
import os
import sys

import numpy as np
from astropy.io import fits
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(FEED, "..", "chandra", "_assessment", "scripts"))
import register as R  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
FOV_GUESSES_ARCMIN = (3.4, 10.0, 1.0)


def refs_for(ra, dec, rfov, work):
    yield "legacy", lambda: R.ref_legacy(ra, dec, rfov, work)
    if dec > -30:
        yield "ps1", lambda: R.ref_ps1(ra, dec, rfov, work)
    yield "sdss", lambda: R.ref_hips("sdss", ra, dec, rfov, work)
    yield "dss2", lambda: R.ref_skyview("DSS2 Red", ra, dec, rfov, 1500, work)
    yield "2mass", lambda: R.ref_hips("2mass", ra, dec, rfov, work)


def main():
    os.chdir(FEED)
    cands = {c["id"]: c for c in json.load(open("candidates.json", encoding="utf-8")) if c.get("png") and c.get("seed")}
    ids = sys.argv[1:] or sorted(cands)
    work = os.path.join(FEED, "_work")
    os.makedirs(work, exist_ok=True)
    os.makedirs("wcs", exist_ok=True)
    logp = os.path.join("wcs", "_register_log.csv")
    new = not os.path.exists(logp)
    logf = open(logp, "a", newline="", encoding="utf-8")
    lw = csv.writer(logf)
    if new:
        lw.writerow(["id", "result", "ref", "inliers", "rms_arcsec", "px_arcsec", "rms_px", "fov_guess_arcmin", "tried"])
    for uid in ids:
        c = cands[uid]
        if os.path.exists(os.path.join("wcs", uid + ".fits")):
            continue
        img = Image.open(c["png"])
        Wf, Hf = img.size
        z = min(1.0, 4000 / max(Wf, Hf))
        C = R.to_gray8(np.asarray(img.convert("L").resize((int(Wf * z), int(Hf * z)), Image.LANCZOS)))
        ra, dec = c["seed"]["ra"], c["seed"]["dec"]
        tried, result = [], None
        for fov_am in FOV_GUESSES_ARCMIN:
            fov = fov_am / 60
            cs_est = fov * 3600 / C.shape[1]
            rfov = min(max(fov * 2.5, 8 / 60), 1.0)
            for name, mk in refs_for(ra, dec, rfov, work):
                try:
                    ref = mk()
                except Exception as e:
                    tried.append(f"{fov_am}' {name}: error {str(e)[:60]}")
                    continue
                if ref is None:
                    tried.append(f"{fov_am}' {name}: no data")
                    continue
                sol, msg = R.solve(C, Wf, Hf, cs_est, ref)
                tried.append(f"{fov_am}' {msg}")
                if sol:
                    result = (sol, fov_am)
                    break
            if result:
                break
        if result:
            (CW, info), fov_am = result
            hdr = CW.to_header()
            hdr["IMAGEW"] = Wf
            hdr["IMAGEH"] = Hf
            hdr["WCSSRC"] = info["ref"][:60]
            hdr["NMATCH"] = info["inliers"]
            hdr["RMSARCS"] = info["rms_arcsec"]
            fits.PrimaryHDU(header=hdr).writeto(os.path.join("wcs", uid + ".fits"), overwrite=True)
            lw.writerow([uid, "ok", info["ref"], info["inliers"], info["rms_arcsec"], info["px_arcsec"],
                         info["rms_px"], fov_am, " || ".join(tried)])
            print(uid, "OK", info, flush=True)
        else:
            lw.writerow([uid, "failed", "", "", "", "", "", "", " || ".join(tried)])
            print(uid, "FAILED", " || ".join(tried)[:200], flush=True)
        logf.flush()


if __name__ == "__main__":
    main()
