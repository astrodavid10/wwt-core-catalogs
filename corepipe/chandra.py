# Copyright 2026 the .NET Foundation
# Licensed under the MIT License

"""
An image source that scrapes the Chandra X-ray Center photo album
(``https://chandra.harvard.edu/photo/YYYY/<slug>/``).

Chandra does not run a Djangoplicity site and most releases since ~2018 carry
no (or only position-only) AVM, so positioning comes from one of two places:

1. Full AVM WCS embedded in the release's TIFF (or, failing that, in its
   small JPEG, rescaled to the TIFF size).
2. A sidecar FITS header ``<feed_dir>/<wcs_dir>/<id>.fits`` holding a TAN WCS
   in FITS (bottom-up) pixel convention for the downloaded image. These are
   made by registering the image against a reference with AVM/FITS WCS
   (``feeds/chandra/_assessment/scripts/register.py``). A sidecar wins over
   embedded AVM, so a bad AVM tag can be overridden.

Candidate IDs are ``<year>-<slug>``, e.g. ``2026-m101``.

Config keys (under ``chandra:``):

``base_url`` (optional)
    Defaults to ``https://chandra.harvard.edu/photo/``.
``first_year`` / ``last_year``
    Range of year index pages to scan for releases.
``wcs_dir`` (optional)
    Sidecar WCS directory, relative to the feed directory. Default ``wcs``.
"""

import html
import json
import os
import re
import shutil
import time
import warnings
from typing import BinaryIO, Iterable, Optional

import numpy as np
import requests
from toasty.builder import Builder
from toasty.image import ImageLoader

from .base import CandidateInput, ImageSource, NotActionableError

__all__ = """
ChandraImageSource
ChandraCandidateInput
parse_release_page
""".split()

UA = {"User-Agent": "wwt-core-catalogs corepipe (chandra feed)"}

FACT_LABELS = [
    "Credit",
    "Release Date",
    "Scale",
    "Category",
    r"Coordinates \(J2000\)",
    "Constellation",
    "Observation Dates?",
    "Observation Time",
    r"Obs\. ID",
    "Instrument",
    "Also Known As",
    "References",
    "Color Code",
    "Distance Estimate",
    "Distance",
]


def _get(url: str, stream: bool = False, none404: bool = False) -> Optional[requests.Response]:
    for attempt in range(5):
        try:
            resp = requests.get(url, stream=stream, headers=UA, timeout=120)
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            resp.close()
            time.sleep(5 * (attempt + 1))
            continue
        break
    if none404 and resp.status_code == 404:
        return None
    if not resp.ok:
        raise Exception(f"error fetching url `{url}`: {resp.status_code}")
    return resp


def _text(h: str) -> str:
    h = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", h, flags=re.S)
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))
    return t.replace("\xa0", " ").strip()


def _facts(txt: str) -> dict:
    i = txt.find("Fast Facts")
    t = txt[i:] if i >= 0 else txt
    pat = re.compile(r"(?<![A-Za-z])(" + "|".join(FACT_LABELS) + r")\s*[:�]?\s")
    ms = list(pat.finditer(t))
    out = {}
    for k, m in enumerate(ms):
        lab = m.group(1).replace("\\", "")
        end = ms[k + 1].start() if k + 1 < len(ms) else m.end() + 400
        v = re.sub(r"(\s*-->)+", "", t[m.end() : end]).strip(" -—:�>").strip()
        if lab not in out and v:
            out[lab] = v
    # The last field runs into the page boilerplate; trim at the first sentence.
    for lab in ("Distance Estimate", "Distance"):
        if lab in out:
            out[lab] = re.split(r"(?<=Earth)|(?<=years)\b", out[lab])[0].strip()
    return out


def parse_release_page(h: str) -> dict:
    """Pull title, description, fact sheet and download links from a release page."""
    info = {}
    m = re.search(r"<title>(.*?)</title>", h, re.S)
    title = _text(m.group(1)) if m else ""
    title = re.sub(r"^Chandra :: Photo Album :: ", "", title)
    info["Title"] = re.sub(r"\s*::.*$", "", title).strip()

    info["facts"] = _facts(_text(h))

    # Description: the opening paragraphs of the release text.
    paras = []
    for p in re.findall(r"<p>(.*?)</p>", h, re.S):
        t = _text(p)
        if len(t) < 80:
            continue
        paras.append(t)
        if len(paras) == 2:
            break
    info["Description"] = "\n\n".join(paras)

    dls = []
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
        href, label = m.group(1), _text(m.group(2))
        if "/" in href.strip("./"):
            continue
        if re.search(r"\.(tiff?|jpe?g)$", href, re.I) and not href.endswith("_thm.jpg"):
            dls.append((href, label))
    info["downloads"] = dls

    tif = lg = jpg = None
    for href, label in dls:
        ll = label.lower()
        if ll.startswith("tif") and "(" in ll and not tif:
            tif = href
        elif ll.startswith("large jpeg") and not lg:
            lg = href
        elif ll.startswith("jpeg (") and not jpg:
            jpg = href
    if not tif:
        tif = next((x for x, _ in dls if re.search(r"\.tiff?$", x, re.I)), None)
    info["files"] = {"tif": tif, "lg": lg, "jpg": jpg}
    return info


def _xmp_packets(path: str) -> list:
    """Raw XMP packets near the start and end of a file (TIFF/JPEG)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(min(size, 8 << 20))
        tail = b""
        if size > len(head):
            f.seek(max(len(head), size - (8 << 20)))
            tail = f.read()
    out = []
    for blob in (head, tail):
        s = blob.decode("latin-1")
        for m in re.finditer(r"<x:xa?mpmeta.*?</x:xa?mpmeta>", s, re.S):
            out.append(m.group(0))
    return out


def avm_sets(path: str) -> list:
    """All AVM Spatial tag sets found in a file, one dict per rdf:Description block.

    Chandra TIFFs sometimes hold several (e.g. one written for a 300-px preview),
    which confuses pyavm, so we parse them ourselves.
    """
    sets = []
    for pkt in _xmp_packets(path):
        blocks = re.findall(r"<rdf:Description\b[^>]*?/>|<rdf:Description\b[^>]*>.*?</rdf:Description>", pkt, re.S)
        for blk in blocks:
            d = {}
            for k, v in re.findall(r'avm:(Spatial\.\w+)="([^"]*)"', blk):
                d[k] = v
            for k, v in re.findall(r"<avm:(Spatial\.\w+)>(.*?)</avm:\1>", blk, re.S):
                d[k] = v
            vals = {}
            for k, v in d.items():
                v = re.sub(r"<[^>]+>", " ", v).replace(";", " ").replace(",", " ")
                vals[k] = v.split()
            if "Spatial.ReferenceValue" in vals:
                sets.append(vals)
    return sets


def _f(v):
    return float(re.sub(r"[^0-9eE+\-.]", "", v))


def sidecar_image_wcs(hdr):
    """WCS for a registration sidecar, in the image's own (top-down) pixel convention.

    The sidecars (``register.py``) are written in the FITS convention, with y counting
    up from the bottom row. Images loaded from JPEG/PNG/TIFF count rows down from the
    top, so the y axis is flipped here: CRPIX2 -> H + 1 - CRPIX2 and the second
    column of the CD matrix changes sign. Handing toasty the sidecar unchanged makes
    it "fix" the parity by flipping the pixels, which mirrors the picture top to
    bottom on the sky.
    """
    from astropy.wcs import WCS

    src = WCS(hdr).celestial
    height = int(hdr["IMAGEH"])
    cd = src.pixel_scale_matrix.copy()
    cd[:, 1] *= -1
    w = WCS(naxis=2)
    w.wcs.ctype = list(src.wcs.ctype)
    w.wcs.crval = list(src.wcs.crval)
    w.wcs.crpix = [src.wcs.crpix[0], height + 1 - src.wcs.crpix[1]]
    w.wcs.cd = cd
    return w


def avm_wcs_headers(s: dict, width: int, height: int):
    """FITS headers in image (JPEG-like, negative parity) convention, or (None, reason).

    Only accepts a set that is consistent with the actual image: same aspect ratio
    (within 1%), square pixels (within 1%) and a TAN/SIN projection. Anything
    else is better registered than trusted.
    """
    try:
        proj = (s.get("Spatial.CoordsystemProjection") or ["TAN"])[0].upper()
        if proj not in ("TAN", "SIN"):
            return None, f"projection {proj}"
        ra, dec = [_f(x) for x in s["Spatial.ReferenceValue"][:2]]
        if "Spatial.ReferencePixel" not in s:
            return None, "no ReferencePixel"
        px, py = [_f(x) for x in s["Spatial.ReferencePixel"][:2]]
        if "Spatial.ReferenceDimension" in s:
            rw, rh = [_f(x) for x in s["Spatial.ReferenceDimension"][:2]]
        else:
            rw, rh = float(width), float(height)
        f0, f1 = width / rw, height / rh
        if abs(f0 - f1) / max(f0, f1) > 0.01:
            return None, f"ReferenceDimension {rw:.0f}x{rh:.0f} does not match image {width}x{height}"
        if "Spatial.Scale" in s and len(s["Spatial.Scale"]) >= 2:
            sx, sy = _f(s["Spatial.Scale"][0]), _f(s["Spatial.Scale"][1])
            rot = _f((s.get("Spatial.Rotation") or ["0"])[0])
            if abs(abs(sx) - abs(sy)) / max(abs(sx), abs(sy)) > 0.02:
                return None, f"non-square Scale {sx:.3g},{sy:.3g}"
            m = (abs(sx) + abs(sy)) / 2  # WWT needs square pixels
            sx, sy = np.copysign(m, sx), np.copysign(m, sy)
            c, sn = np.cos(np.radians(rot)), np.sin(np.radians(rot))
            lam = sy / sx
            cd = np.array([[sx * c, -sx * lam * sn], [sy * sn / lam, sy * c]])
        elif "Spatial.CDMatrix" in s and len(s["Spatial.CDMatrix"]) >= 4:
            cd = np.array([_f(x) for x in s["Spatial.CDMatrix"][:4]]).reshape(2, 2)
            a, b = np.hypot(cd[0, 0], cd[1, 0]), np.hypot(cd[0, 1], cd[1, 1])
            if abs(a - b) / max(a, b) > 0.02:
                return None, "non-square CDMatrix"
        else:
            return None, "no Scale/CDMatrix"
    except (KeyError, ValueError, IndexError) as e:
        return None, f"unparseable AVM ({e})"

    h = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": ra,
        "CRVAL2": dec,
        "CRPIX1": (px - 0.5) * f0 + 0.5,
        "CRPIX2": (py - 0.5) * f1 + 0.5,
    }
    cd = cd / np.array([[f0, f0], [f1, f1]])
    # AVM is FITS-like (y up); flip to the image's top-down layout, as in djangoplicity.py
    h["CD1_1"], h["CD1_2"] = cd[0, 0], -cd[0, 1]
    h["CD2_1"], h["CD2_2"] = cd[1, 0], -cd[1, 1]
    h["CRPIX2"] = height + 1 - h["CRPIX2"]
    return h, f"AVM ({proj})"


def best_avm(paths: list, width: int, height: int):
    """First usable AVM set across files; returns (headers, note) or (None, reasons)."""
    reasons = []
    for p in paths:
        if not os.path.exists(p):
            continue
        sets = avm_sets(p)
        # prefer a set whose ReferenceDimension equals the image size
        sets.sort(key=lambda s: 0 if [x.split(".")[0] for x in s.get("Spatial.ReferenceDimension", [])[:2]] == [str(width), str(height)] else 1)
        for s in sets:
            h, why = avm_wcs_headers(s, width, height)
            if h is not None:
                return h, f"{why} from {os.path.basename(p)}"
            reasons.append(f"{os.path.basename(p)}: {why}")
    return None, "; ".join(reasons) or "no AVM Spatial tags"


class ChandraImageSource(ImageSource):
    _base_url: str
    _first_year: int
    _last_year: int
    _wcs_dir: str

    @classmethod
    def get_config_key(cls) -> str:
        return "chandra"

    @classmethod
    def deserialize(cls, data: dict) -> "ChandraImageSource":
        inst = cls()
        inst._base_url = data.get("base_url", "https://chandra.harvard.edu/photo/")
        inst._first_year = int(data.get("first_year", 2018))
        inst._last_year = int(data.get("last_year", time.gmtime().tm_year))
        inst._wcs_dir = data.get("wcs_dir", "wcs")
        return inst

    def query_candidates(self) -> Iterable["ChandraCandidateInput"]:
        for year in range(self._first_year, self._last_year + 1):
            url = f"{self._base_url}{year}/"
            print(f"requesting {url} ...")
            resp = _get(url, none404=True)
            if resp is None:
                continue
            seen = []
            pat = r'href="(?:https?://chandra\.harvard\.edu)?/photo/%d/([A-Za-z0-9_\-]+)/(?:index\.html)?"' % year
            for m in re.finditer(pat, resp.text):
                if m.group(1) not in seen:
                    seen.append(m.group(1))
            for slug in seen:
                yield ChandraCandidateInput({"year": year, "slug": slug, "id": f"{year}-{slug}"})

    def _release_url(self, unique_id: str) -> str:
        year, slug = unique_id.split("-", 1)
        return f"{self._base_url}{year}/{slug}/"

    def fetch_candidate(self, unique_id: str, cand_data_stream: BinaryIO, cachedir: str):
        url = self._release_url(unique_id)
        info = parse_release_page(_get(url).text)
        files = info["files"]
        main = files["tif"] or files["lg"]
        if not main:
            raise NotActionableError("release offers no TIFF or large JPEG")

        ext = main.rsplit(".", 1)[-1].lower()
        ext = "tif" if ext == "tiff" else ext
        info["toasty_image_extension"] = ext
        info["ReferenceURL"] = url
        info["image_url"] = url + main
        info["Credit"] = info["facts"].get("Credit", "NASA/CXC/SAO")

        dest = os.path.join(cachedir, "image." + ext)
        for attempt in range(5):
            try:
                with _get(url + main, stream=True) as resp:
                    with open(dest + ".part", "wb") as f:
                        shutil.copyfileobj(resp.raw, f, 1 << 20)
                os.replace(dest + ".part", dest)
                break
            except (requests.RequestException, OSError):
                if attempt == 4:
                    raise
                time.sleep(10 * (attempt + 1))

        # The small JPEG sometimes carries AVM that the TIFF lacks.
        if files["jpg"] and files["jpg"] != main:
            try:
                r = _get(url + files["jpg"])
                with open(os.path.join(cachedir, "avm.jpg"), "wb") as f:
                    f.write(r.content)
            except Exception:
                pass

        with open(os.path.join(cachedir, "metadata.json"), "wt", encoding="utf8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

    def _sidecar(self, unique_id: str, cachedir: str) -> Optional[str]:
        feed_dir = os.path.dirname(os.path.dirname(os.path.abspath(cachedir)))
        p = os.path.join(feed_dir, self._wcs_dir, unique_id + ".fits")
        return p if os.path.exists(p) else None

    def process(self, unique_id: str, cand_data_stream: BinaryIO, cachedir: str, builder: Builder):
        with open(os.path.join(cachedir, "metadata.json"), "rt", encoding="utf8") as f:
            info = json.load(f)

        img_path = os.path.join(cachedir, "image." + info["toasty_image_extension"])
        img = ImageLoader().load_path(img_path)
        try:
            img.aspil().info.pop("xmp", None)  # see djangoplicity.py
        except Exception:
            pass

        sidecar = self._sidecar(unique_id, cachedir)
        wcs = None
        headers = None

        if sidecar:
            from astropy.io import fits
            from astropy.wcs import WCS

            with fits.open(sidecar) as hdul:
                hdr = hdul[0].header.copy()
            ww, wh = hdr.get("IMAGEW"), hdr.get("IMAGEH")
            if (ww, wh) != (img.width, img.height):
                raise Exception(
                    f"sidecar WCS is for a {ww}x{wh} image but {img_path} is {img.width}x{img.height}"
                )
            info["wcs_source"] = f"sidecar {os.path.basename(sidecar)}: {hdr.get('WCSSRC', '')}"
            wcs = sidecar_image_wcs(hdr)
            img._wcs = wcs
            # A press image is a sky view, so after the conversion this should be a
            # no-op; if it ever flips, the picture was published mirrored.
            img.ensure_negative_parity()
        else:
            headers, note = best_avm([img_path, os.path.join(cachedir, "avm.jpg")], img.width, img.height)
            if headers is None:
                raise Exception(f"no usable WCS ({note}); register it to make wcs/{unique_id}.fits")
            info["wcs_source"] = note

        builder.tile_base_as_study(img)
        builder.make_thumbnail_from_other(img)

        if wcs is not None:
            builder.apply_wcs_info(img.wcs, img.width, img.height)
        else:
            builder.imgset.set_position_from_wcs(headers, img.width, img.height, place=builder.place)

        builder.set_name(info["Title"])
        builder.imgset.credits_url = info["ReferenceURL"]
        builder.imgset.credits = html.escape(info["Credit"])
        builder.place.description = html.escape(info.get("Description", ""))

        with open(os.path.join(cachedir, "metadata.json"), "wt", encoding="utf8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

        builder.cascade()


class ChandraCandidateInput(CandidateInput):
    _info: dict

    def __init__(self, info: dict):
        self._info = info

    def get_unique_id(self) -> str:
        return self._info["id"]

    def save(self, stream: BinaryIO):
        stream.write(json.dumps(self._info).encode("utf8"))
