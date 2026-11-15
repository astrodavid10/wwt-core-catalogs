"""Re-tile study images placed with a registration sidecar that was read in the wrong
pixel convention (mirrored top to bottom on the sky), and fix their catalogue entries.

    python feeds/_tools/fix_registered_parity.py tile FEED_DIR IMAGE_TEMPLATE ID [ID ...]
    python feeds/_tools/fix_registered_parity.py db REPO_DIR FEED_DIR ID [ID ...]

tile: for each id, loads FEED_DIR/<IMAGE_TEMPLATE with {id}> and FEED_DIR/wcs/<id>.fits,
      converts the sidecar to the image's top-down convention
      (corepipe.chandra.sidecar_image_wcs), tiles into FEED_DIR/processed_fixed/<id>/,
      and writes index.wtml there: the existing processed/<id>/index.wtml with only
      its geometry (centre, rotation, offsets, scale, levels; Place RA/Dec/zoom)
      replaced. Then moves processed/<id> to _flipped/<id> and processed_fixed/<id>
      into its place.
db:   copies the same geometry from FEED_DIR/processed/<id>/index.wtml into the
      imageset (matched by tile URL) and the places that show it, then rewrites
      imagesets/ and places/ with cattool.

Run with the wwt env python from the repo root.
"""

import os
import shutil
import sys

from astropy.io import fits
from toasty.builder import Builder
from toasty.image import ImageLoader
from toasty.pyramid import PyramidIO
from wwt_data_formats.folder import Folder
from wwt_data_formats.place import Place

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from corepipe.chandra import sidecar_image_wcs  # noqa: E402

IMG_FIELDS = ("center_x", "center_y", "rotation_deg", "offset_x", "offset_y",
              "base_degrees_per_tile", "base_tile_level", "tile_levels", "width_factor")
PLACE_FIELDS = ("ra_hr", "dec_deg", "zoom_level")


def first_place(path):
    f = Folder.from_file(path)
    pl = next(c for c in f.children if isinstance(c, Place))
    return f, pl, (pl.foreground_image_set or pl.image_set)


def tile(feed, template, ids):
    for uid in ids:
        old = os.path.join(feed, "processed", uid, "index.wtml")
        if not os.path.exists(old):
            print(uid, "no processed/index.wtml; skipped")
            continue
        hdr = fits.getheader(os.path.join(feed, "wcs", uid + ".fits"))
        img = ImageLoader().load_path(os.path.join(feed, template.format(id=uid)))
        if (hdr["IMAGEW"], hdr["IMAGEH"]) != (img.width, img.height):
            print(uid, "sidecar size mismatch; skipped")
            continue
        img._wcs = sidecar_image_wcs(hdr)
        flipped = img.get_parity_sign() > 0
        img.ensure_negative_parity()
        out = os.path.join(feed, "processed_fixed", uid)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        b = Builder(PyramidIO(out, scheme="LXY", default_format="png"))
        b.tile_base_as_study(img)
        b.make_thumbnail_from_other(img)
        b.apply_wcs_info(img.wcs, img.width, img.height)
        b.cascade()
        b.write_index_rel_wtml()
        _, npl, nim = first_place(os.path.join(out, "index_rel.wtml"))
        f, opl, oim = first_place(old)
        for k in IMG_FIELDS:
            setattr(oim, k, getattr(nim, k))
        for k in PLACE_FIELDS:
            setattr(opl, k, getattr(npl, k))
        with open(os.path.join(out, "index.wtml"), "wb") as fh:
            f.write_xml(fh, dest_wants_bytes=True)
        os.makedirs(os.path.join(feed, "_flipped"), exist_ok=True)
        shutil.rmtree(os.path.join(feed, "_flipped", uid), ignore_errors=True)
        os.replace(os.path.join(feed, "processed", uid), os.path.join(feed, "_flipped", uid))
        os.replace(out, os.path.join(feed, "processed", uid))
        print(uid, "re-tiled" + (" (image itself is mirrored: flipped)" if flipped else ""), flush=True)


def db(repo, feed, ids):
    sys.path.insert(0, repo)
    os.chdir(repo)
    import cattool

    idb = cattool.ImagesetDatabase()
    pdb = cattool.PlaceDatabase()
    by_url = {}
    for info in pdb.by_uuid.values():
        u = info.get("foreground_image_set_url") or info.get("image_set_url")
        if u:
            by_url.setdefault(u, []).append(info)
    n_img = n_pl = 0
    for uid in ids:
        _, npl, nim = first_place(os.path.join(feed, "processed", uid, "index.wtml"))
        img = idb.by_url.get(nim.url)
        if img is None:
            print(uid, "not in the imageset database:", nim.url)
            continue
        for k in IMG_FIELDS:
            setattr(img, k, getattr(nim, k))
        n_img += 1
        for info in by_url.get(nim.url, []):
            for k in PLACE_FIELDS:
                info[k] = getattr(npl, k)
            n_pl += 1
    idb.rewrite()
    pdb.rewrite()
    print(f"updated {n_img} imagesets and {n_pl} places")


def main():
    mode = sys.argv[1]
    if mode == "tile":
        tile(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif mode == "db":
        db(os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]), sys.argv[4:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
