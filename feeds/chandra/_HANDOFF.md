# Chandra feed: hand-off

## 2026-11-14: first batch (2018–2026 photo-album releases)

### What exists

- **Image source.** `corepipe/chandra.py`, `source_type: chandra`, config in `corepipe-config.yaml`.
  - Candidates are `<year>-<slug>` from `chandra.harvard.edu/photo/YYYY/`.
  - `fetch` downloads the release's full TIFF, plus the small JPEG as an alternative AVM source, and the fact sheet. The fact sheet goes into `metadata.json` as Title, Credit, Description, `facts`.
  - `process` places the image from `wcs/<id>.fits` (a registered WCS file) if present. Otherwise it uses embedded AVM, but only an AVM set consistent with the actual TIFF: matching aspect, square pixels, TAN/SIN.
- **Tiles.** 64 scenes, 1.4 GB, in `processed/<id>/`, with upload-ready `index.wtml` (https URLs, `Xcorepipe_ids=chandra|<id>`, `BandPass="XRay"`). The merged batch is `_merge/chandra-new-2026-11-merged.wtml` (64 places, 0 text artifacts).
- **Manifest.** `_batch_2026-11.csv` lists, per scene:
  - name;
  - all bands, for `Xbands` at ingest;
  - where its WCS came from;
  - whether the black-border fix was applied;
  - tile size;
  - existing Chandra imagesets of the same field, so you can decide whether the new scene replaces or adds to them.

### Why most of the batch needed registration

Chandra's AVM in 2018+ TIFFs is unreliable:

- 22 of the 42 releases the assessment called "easy" carry AVM written for another crop or size.
- Some have outright errors:
  - `2021-gcenter` is in Galactic coordinates with no frame tag;
  - `2021-nclump` has Dec −0.53 instead of −52.69;
  - `2026-reaper` has the Dec sign flipped;
  - several scales are off by 2–5×.
- The fact sheets also have errors: NGC 6099's RA is 17′ off, and the fact-sheet field size is wrong about a third of the time.

So every image went through `_assessment/scripts/register.py`, and AVM was kept only where nothing better exists and it agrees with the fact sheet.

### How register.py works

- **Seed.** The AVM centre, or else the fact sheet. The field size comes from the fact sheet, or else the AVM.
- **References, in order:**
  1. the matching press image with AVM (ESA/Hubble, ESA/Webb, ESO, NOIRLab);
  2. Legacy Survey DR10 r, PS1 r, SDSS DR9, DSS2 (via SkyView or the IRSA HiPS), and IRAC1, 2MASS K and WISE W1 (IRSA HiPS);
  3. Chandra's own CDA HiPS (`cdaftp.cfa.harvard.edu/cxc-hips`).
- **Matching.** SIFT with one-to-one matches and a scale-plausibility RANSAC loop. The fallback is a point-pattern search (DoG peaks; grid over scale and rotation; FFT correlation) with a chance-coincidence test.
- **Unavailable services.** CDS hips2fits and the alasky tile servers were down (HTTP 404), and SkyView's SDSS, 2MASS and WISE fail upstream. `hips_sample.py` samples HiPS tiles directly. Its tile layout was checked against PS1, and a render of Cas A from the CDA HiPS looks correct.

### Checks

- **Sanity review.** `review_queue.py`, output in `wcs/_review.csv`.
- **Independent DSS overlay score.** `overlay_check.py`, output in `wcs/_overlay_scores.csv`.
- **Visual review.** By eye for every low-score scene and every thumbnail; decisions are in `wcs/_curation.csv`.
- **Verified against known positions or AVM:**
  - M101: registration and AVM agree to 2–3″.
  - E0102: 10″ from the catalogued remnant, while its AVM is 147″ off.
- **Not cross-checkable by DSS:** fine-pixel scenes registered to Hubble or Webb press frames. Those are trusted because of their inlier counts (hundreds to thousands) and RMS of 0.01–0.25″.

### Black borders

The `feeds/_tools/nodata_alpha.py` survey was run on all 64 scenes:

- **Applied (T=0)** to `2020-ic4593`, `2023-spiders`, `2023-tycho` and `2026-heart`. These have solid edge frames, and 0.0–0.6% of pixels were masked.
- **Skipped**, because the masks are speckled clipped sky (mostly zero-count X-ray background): `2018-kes75`, `2019-a1758`, `2019-coma`, `2020-kepler`, `2026-kepler`, `2024-snr1181`, `2025-g181`, `2026-m101`, `2026-sgrc` and `2026-trebleclef`.
- **Skipped, mixed:** `2026-a2029`. Its rotated-mosaic corners are padding, but the mask runs on into speckled sky.

### Not published

The tiles for these are kept in `on-hold/`; reasons are in `wcs/_curation.csv`.

- **Rejected, 9:**
  - Collages and multi-panel graphics: `imbhs`, `terzan5`, `sonify2`/`3`/`4`/`8`, `timelapse`, `250th`.
  - Duplicate: `sonify6`, which repeats the V404 Cyg image.
- **Held for your decision, 14:**
  - Insets, labels or drawn circles baked into the image: `2018-m51`, `gsn069`, `2021-msh1552`, `nclump`, `g292`, `ngc4424`, `2022-sgra`, `spiderweb`, `uhz1`, `2025-casa`, `ngc6099`, `rcw86`.
  - Unverifiable fits: `2021-j1818` and `2021-sgrae`.
  - Also `2022-j2030` and `2023-ngc253`, which are multi-panel suspects from `review_queue.py`.
- **AVM tiles pulled:** `2021-gcenter`, `2026-reaper` and `2018-ngc6231`, which have bad AVM and no registration. Their old tiles are in `on-hold/*.avm/`.
- **Not registered:** 136 releases, listed in `wcs/_review.csv` (decision `hold`, reason "no registration"). 51 had no usable seed: collections, sonification and illustration releases, or a fact sheet without coordinates or scale. The rest failed matching, mostly X-ray-only fields and heavily processed composites.
- **Not fetched:**
  - `2021-kpd0005`: persistent 503.
  - `2018-gw`, `2023-crabx`, `2024-collide`, `2024-decoupling`: no TIFF offered.

### Next steps (not done: they need your OK)

1. **Upload.** Copy each `processed/<id>/` to `feeds/chandra/<id>/` on data1.wwtassets.org, then run `check_blobs.py` on the merged WTML.
2. **Ingest.**
   - Create `catfiles/chandra.yml` and an empty `cxprep/chandra.txt`.
   - Run `cattool ingest _merge/chandra-new-2026-11-merged.wtml --cx-handle=skip` with a prepend or new catfile.
   - Set `Xbands` from `_batch_2026-11.csv`.
   - Decide which of the 64 replace older Chandra imagesets of the same field (`existing_chandra_in_wwt`).
3. **The 136 unregistered releases.** See "Plate solving: deferred" below.

**Update (later the same day):** the main session merged this branch at a22448c into the fork's main and ingested the 64 scenes as Chandra Studies (26143e7). They were prepended to `catfiles/studieschandra.yml` with `Multi` + `Xbands` from `_batch_2026-11.csv`. Older images of the same field are flagged in a list, not removed. The 14 held images stay out, with their tiles kept in `on-hold/`.

### Plate solving: deferred (2026-11-14)

The user chose to skip plate solving for now. Nothing was installed and no outside service was used. The 136 unregistered releases are listed in `wcs/_review.csv` (decision `hold`, reason "no registration"). Expect a modest yield whichever option is used:

- 51 of the 136 have no usable seed (collections, sonification and illustration releases, or a fact sheet without coordinates or scale).
- Many of the rest are X-ray-only fields with few optical stars.
- A plate solver would mainly help the composites and images with an optical layer: perhaps a few dozen.

astrometry.net can't simply go into a new conda env here. conda-forge builds `astrometry` (0.97) only for Linux/macOS, not win-64, and WSL is enabled but has no distribution installed. The options:

1. **WSL Ubuntu + astrometry.net (recommended).**
   - The user installs a distro: `wsl --install -d Ubuntu` needs admin and may need a reboot.
   - Then create a conda env (e.g. `astrometry`) inside it from conda-forge.
   - Download only the 4100/4200-series index files for the seed scales, roughly 0.3–60′ fields: tens of MB for the 4100 series, about 2 GB with the 4200 tiles near each seed.
   - Run `solve-field --ra --dec --radius --scale-low/--scale-high`, seeded from the AVM or fact sheet (±30% scale).
2. **ASTAP**, a Windows-native plate solver.
   - It needs its installer and a star catalogue (D50, ~1 GB) from hnsky.org, a third-party download the user has to approve.
   - It's fast, with similar position and scale seeding.
3. **nova.astrometry.net web API.**
   - No local install, but it needs the user's API key (an account).
   - The images would be uploaded to an external service. They are public Chandra press images, but that is the user's call.

Whichever is used, accept a solution only if it passes the same checks as the registered batch: the `review_queue.py` scale and centre rules, plus the `overlay_check.py` DSS overlap and a look at the overlay. Then tile, run the `nodata_alpha.py` survey, and build a second batch (`_merge/chandra-new-2026-11b-merged.wtml`, `_batch_2026-11b.csv`).

### Running it again

All commands run from `feeds/chandra`:

```bash
WWTPY=C:/Users/adavi/anaconda3/envs/wwt/python.exe   # needs pyavm (installed 2026-11-14)
$WWTPY ../../pipeline.py refresh
bash _assessment/scripts/fetch_slow.sh <idfile>       # chandra.harvard.edu throttles (503); parks in _hold_nowcs/
bash _assessment/scripts/register_all.sh _hold_nowcs <idfile> <tag>   # base anaconda python (cv2, astropy_healpix)
python _assessment/scripts/review_queue.py [--apply]  # base python
$WWTPY ~/.claude/skills/wwt-catalog-update/scripts/tile_large.py .
python _assessment/scripts/overlay_check.py . <outdir> <cachedir> <ids...>
python feeds/_tools/nodata_alpha.py survey --preview <dir> processed/*   # from main
$WWTPY ~/.claude/skills/wwt-catalog-update/scripts/build_deliverable.py . <ids> --prefix https://data1.wwtassets.org/feeds/chandra/ --tag-feed chandra --merged _merge/... --merged-name ... [--rename "ID=Name" (see _renames.txt)]
```

Windows note: background shells may lack the home variables, which makes astropy and the wwt env fail with "Could not determine home directory". The wrapper scripts export `USERPROFILE`, `HOMEDRIVE` and `HOMEPATH`.
