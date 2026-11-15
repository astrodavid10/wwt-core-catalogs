# Hubble images published only on NASA's sites (processing: Gladys Kober)

These Hubble images exist only on science.nasa.gov (the Hubble Caldwell and Messier
catalogue pages) and www.nasa.gov (weekly image articles). None carry AVM or WCS, so
each one is registered against survey images before tiling. The assessment that led
here is `feeds/hst/nasa_science_assessment/README.md`.

Run from this directory:

1. `tools/inventory.py` (wwt env) writes `candidates.json`: 77 Kober-processed images
   as of 2026-11-15 (40 Caldwell, 24 Messier, 13 weekly). Seed positions come from the
   SIMBAD mirror at simbad.cfa.harvard.edu; `NAMES` in the script fixes pages whose text
   names no resolvable object. Originals go to `_dl/`, PNG copies to `_png/`.
2. `tools/register_nasa.py` (base anaconda: cv2, astropy_healpix) uses
   `feeds/chandra/_assessment/scripts/register.py` against Legacy Survey, PS1, SDSS,
   DSS2 and 2MASS, trying field widths of 3.4', 10' and 1'. Accepted fits are written
   to `wcs/<id>.fits`, and every attempt is logged in `wcs/_register_log.csv`.
3. `tools/overlay_check_nasa.py` renders DSS2 on each image's grid for a visual check.
4. `tools/tile_nasa.py` (wwt env) tiles into `processed/<id>/` and writes `index.wtml`
   with https URLs under `data1.wwtassets.org/feeds/hst/<id>/`, plus the merged WTML.

## 2026-11 batch

- **Registered:** 29 of 76 (one more, the Roman lens field, names no object). The fitted
  scales are standard Hubble values (0.0396, 0.050 and 0.100"/px); each fit was checked
  by eye against DSS2.
- **Ingested:** prepended to `catfiles/studieshubble.yml`. Bands come from
  `bandpass/apply_bands_to.py`, with `nasa-messier-55=Visible` set by hand (an author's
  affiliation mentions JWST).
- **Duplicate check:** five overlap older Hubble images of M3, M4, M5, M64 and M80. Each
  new one covers a wider field, and the M80 image is also sharper, so all are kept.
- **Not registered (47):** listed in `wcs/_register_log.csv`, mostly bright galaxy cores
  and crowded cluster centres that the surveys show as one saturated blob. A plate solver
  (astrometry.net or ASTAP) or manual alignment would be the next step.
