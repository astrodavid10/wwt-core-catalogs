"""Tile registered NASA-only Hubble images and write upload-ready WTMLs.

    python tools/tile_nasa.py [id ...]      (wwt env python, run from feeds/hst_nasa)

For each id with wcs/<id>.fits (and not listed in skip.txt): tile _png/<id>.png as a
TAN study into processed/<id>/ (toasty LXY PNG pyramid), thumbnail, and write
processed/<id>/index.wtml with https URLs under
https://data1.wwtassets.org/feeds/hst/<id>/ (the tiles live beside the ESA/Hubble
feed). Text comes from candidates.json: title, page caption + summary, credit,
and CreditsUrl = the NASA page. Then writes _merge/hst-nasa-<date>-merged.wtml.
"""

import html
import json
import os
import re
import sys
import unicodedata

from astropy.io import fits
from toasty.builder import Builder
from toasty.image import ImageLoader
from toasty.pyramid import PyramidIO
from wwt_data_formats.folder import Folder, make_absolutizing_url_mutator
from wwt_data_formats.place import Place

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
from corepipe.chandra import sidecar_image_wcs  # noqa: E402  (FITS y-up sidecar -> top-down image WCS)

PREFIX = "https://data1.wwtassets.org/feeds/hst/"
FEED_TAG = "hst_nasa"


def clean(s):
    s = html.unescape(s or "").replace(" ", " ")
    return unicodedata.normalize("NFC", " ".join(s.split()))


def credit_text(c):
    s = clean(c["credit"])
    s = re.sub(r"^(Image )?[Cc]redits?:\s*", "", s)
    return s


def description(c):
    cap, summ = clean(c["caption"]), clean(c["summary"])
    if cap and summ and cap[:60] in summ:
        return summ
    return " ".join(x for x in (cap, summ) if x)


def name_for(c):
    t = clean(c["title"])
    if c["id"].startswith(("nasa-caldwell-", "nasa-messier-")):
        alt = c["seed"]["name"] if c.get("seed") else ""
        if alt and not alt.startswith(("M ", "Messier", "NAME ")) and alt not in t:
            t = f"{t} ({alt})"
    return t


def main():
    feed = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(feed)
    cands = {c["id"]: c for c in json.load(open("candidates.json", encoding="utf-8"))}
    skip = set(open("skip.txt").read().split()) if os.path.exists("skip.txt") else set()
    ids = sys.argv[1:] or sorted(i for i in cands if os.path.exists(f"wcs/{i}.fits") and i not in skip)
    folder = Folder()
    folder.name = "Hubble (NASA-processed)"
    for uid in ids:
        c = cands[uid]
        out = os.path.join("processed", uid)
        if not os.path.exists(os.path.join(out, "index_rel.wtml")):
            hdr = fits.getheader(f"wcs/{uid}.fits")
            img = ImageLoader().load_path(c["png"])
            if (hdr["IMAGEW"], hdr["IMAGEH"]) != (img.width, img.height):
                print(uid, "size mismatch; skipped")
                continue
            img._wcs = sidecar_image_wcs(hdr)
            img.ensure_negative_parity()
            os.makedirs(out, exist_ok=True)
            b = Builder(PyramidIO(out, scheme="LXY", default_format="png"))
            b.tile_base_as_study(img)
            b.make_thumbnail_from_other(img)
            b.apply_wcs_info(img.wcs, img.width, img.height)
            b.set_name(name_for(c))
            b.imgset.credits_url = c["source"]
            b.imgset.credits = credit_text(c)
            b.place.description = description(c)
            b.cascade()
            b.write_index_rel_wtml()
            print(uid, "tiled", flush=True)
        f = Folder.from_file(os.path.join(out, "index_rel.wtml"))
        f.mutate_urls(make_absolutizing_url_mutator(PREFIX + uid + "/"))
        for ch in f.children:
            if isinstance(ch, Place):
                im = ch.foreground_image_set or ch.image_set
                im.xmeta.corepipe_ids = f"{FEED_TAG}|{uid}"
                folder.children.append(ch)
        with open(os.path.join(out, "index.wtml"), "wb") as fh:
            f.write_xml(fh, dest_wants_bytes=True)
    os.makedirs("_merge", exist_ok=True)
    mp = "_merge/hst-nasa-2026-11-merged.wtml"
    with open(mp, "wb") as fh:
        folder.write_xml(fh, dest_wants_bytes=True)
    txt = open(mp, encoding="utf-8").read()
    bad = len(re.findall(r'(Url|Thumbnail)="http://', txt)) + txt.count("&amp;amp;")
    print(f"merged {len(folder.children)} places -> {mp}; artifacts: {bad}")


if __name__ == "__main__":
    main()
