# Copyright the WorldWide Telescope project
# Licensed under the MIT License.

"""
Support for loading images from a Djangoplicity database.
"""

__all__ = """
DjangoplicityImageSource
DjangoplicityCandidateInput
""".split()

import ast
import codecs
from contextlib import contextmanager
from datetime import datetime, timezone
import html
import json
import numpy as np
import os.path
import requests
import shutil
from typing import BinaryIO, Iterable, Optional
from urllib.parse import quote as urlquote
import yaml

from toasty.builder import Builder
from toasty.image import ImageLoader

from .base import CandidateInput, ImageSource, NotActionableError


def fix_djangoplicity_json(obj):
    """
    Some versions/installations of Djangoplicity have a bug where textual data
    come through in the API JSON content as the repr of a Python `bytes`, rather
    than actual Unicode text. E.g. for heic2405a, we currently get:

    { ... "Title": "b'Brown dwarf (artist\\xe2\\x80\\x99s concept)'" ... }

    This function cleans that up.
    """

    if isinstance(obj, str):
        if not obj.startswith("b'"):
            return obj

        # We (almost definitely) have a byte-string repr
        try:
            return ast.literal_eval(obj).decode("utf-8")
        except:
            return obj  # least-bad way to handle this, I think

    if isinstance(obj, list):
        return [fix_djangoplicity_json(x) for x in obj]

    if isinstance(obj, dict):
        return dict((k, fix_djangoplicity_json(v)) for k, v in obj.items())

    return obj


class DjangoplicityImageSource(ImageSource):
    """
    An ImageSource that obtains its inputs from a query to a Djangoplicity website.
    """

    _base_url: str
    _search_page_name: str

    @classmethod
    def get_config_key(cls) -> str:
        return "djangoplicity"

    @classmethod
    def deserialize(cls, data: dict) -> "DjangoplicityImageSource":
        inst = cls()
        inst._base_url = data["base_url"]
        inst._search_page_name = data.get("search_page_name", "page")
        return inst

    @contextmanager
    def make_request(
        self, url: str, stream: bool = False, none404: bool = False
    ) -> Iterable[Optional[BinaryIO]]:
        with requests.get(url, stream=stream) as resp:
            if none404 and resp.status_code == 404:
                yield None
                return

            if not resp.ok:
                raise Exception(f"error fetching url `{url}`: {resp.status_code}")

            if stream:
                # By default, `resp.raw` does not perform content decoding.
                # eso.org gives us gzipped content. The following bit is
                # apparently the preferred workaround. Cf:
                # https://github.com/psf/requests/issues/2155
                #
                # A side effect of the content decoding, however, is that the
                # first read of the stream can return a zero-length string,
                # which causes `readlines` iteration to exit. Callers must be
                # prepared to handle this.
                resp.raw.decode_content = True

            yield resp

    def query_candidates(self) -> Iterable["DjangoplicityCandidateInput"]:
        page_num = 1

        while True:
            url = (
                self._base_url
                + f"archive/search/{self._search_page_name}/{page_num}/?type=Observation"
            )
            print(f"requesting {url} ...")

            with self.make_request(url, stream=True, none404=True) as resp:
                if resp is None:
                    break  # got a 404 -- all done

                text_stream = codecs.getreader("utf8")(resp.raw)
                json_lines = []

                # Cf. stream=True in make_request -- skip the zero-length result
                # to prevent readlines iteration from exiting early. This is
                # definitely OK since our `var images` line won't be the first
                # line.
                text_stream.readline()

                for line in text_stream:
                    if not len(json_lines):
                        if "var images = [" in line:
                            json_lines.append("[")
                    elif "];" in line:
                        json_lines.append("]")
                        break
                    else:
                        json_lines.append(line)

            if not len(json_lines):
                raise Exception(
                    f'error processing url {url}: no "var images" data found'
                )

            # This is really a JS literal, but YAML is compatible enough.
            # JSON does *not* work because the dict keys here aren't quoted.
            data = yaml.safe_load("".join(json_lines))

            for item in data:
                yield DjangoplicityCandidateInput(item)

            page_num += 1

    def fetch_candidate(
        self, unique_id: str, _cand_data_stream: BinaryIO, cachedir: str
    ):
        url = self._base_url + urlquote(unique_id) + "/api/json/"

        with self.make_request(url) as resp:
            info = json.loads(resp.content)

        # Clean up the Python bytes reprs, potentially
        info = fix_djangoplicity_json(info)

        # Find the "fullsize original" image URL

        fullsize_url = None

        for resource in info["Resources"]:
            if resource.get("ResourceType") == "Original":
                fullsize_url = resource["URL"]
                break

        if fullsize_url is None:
            raise Exception(
                f'error processing {unique_id}: can\'t identify "fullsize original" image URL'
            )

        ext = fullsize_url.rsplit(".", 1)[-1].lower()
        info["toasty_image_extension"] = ext

        # Validate that there's actually WCS we can use

        if not isinstance(info.get("Spatial.CoordsystemProjection", None), str):
            raise NotActionableError("image does not have full WCS")

        # Download it

        with self.make_request(fullsize_url, stream=True) as resp:
            with open(os.path.join(cachedir, "image." + ext), "wb") as f:
                shutil.copyfileobj(resp.raw, f)

        with open(os.path.join(cachedir, "metadata.json"), "wt", encoding="utf8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

    def process(
        self,
        unique_id: str,
        _cand_data_stream: BinaryIO,
        cachedir: str,
        builder: Builder,
    ):
        # Set up the metadata.

        with open(os.path.join(cachedir, "metadata.json"), "rt", encoding="utf8") as f:
            info = json.load(f)

        img_path = os.path.join(cachedir, "image." + info["toasty_image_extension"])
        md = DjangoplicityMetadata(info)

        # Load up the image.

        img = ImageLoader().load_path(img_path)

        # Some AVM TIFFs carry an XMP packet that Pillow reads as a one-element
        # tuple rather than bytes. Pillow's JPEG writer then crashes when saving
        # the thumbnail (`can't concat tuple to bytes`). We don't need the XMP
        # downstream, so drop it from the underlying PIL image.
        try:
            img.aspil().info.pop("xmp", None)
        except Exception:
            pass

        # Do the processing.

        builder.tile_base_as_study(img)
        builder.make_thumbnail_from_other(img)

        builder.imgset.set_position_from_wcs(
            md.as_wcs_headers(img.width, img.height),
            img.width,
            img.height,
            place=builder.place,
        )

        builder.set_name(info["Title"])
        builder.imgset.credits_url = info["ReferenceURL"]
        builder.imgset.credits = html.escape(info["Credit"])
        builder.place.description = html.escape(info["Description"])

        # Annotation metadata

        ### pub_dt = datetime.fromisoformat(info["Date"])
        ### if pub_dt.tzinfo is None:
        ###     pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        # Finally, crunch the rest of the pyramid.

        builder.cascade()


class DjangoplicityCandidateInput(CandidateInput):
    """
    A CandidateInput obtained from an AstroPix query.
    """

    _info: dict

    def __init__(self, info: dict):
        self._info = info

    def get_unique_id(self) -> str:
        return self._info["id"]

    def save(self, stream: BinaryIO):
        with codecs.getwriter("utf8")(stream) as text_stream:
            json.dump(self._info, text_stream, ensure_ascii=False, indent=2)


class DjangoplicityMetadata(object):
    metadata: dict

    def __init__(self, metadata: dict):
        self.metadata = metadata

    def as_wcs_headers(self, width: int, height: int) -> dict:
        """
        The metadata here are essentially AVM headers. As described in
        `Builder.apply_avm_info()`, the data that we've seen in the wild are a
        bit wonky with regards to parity: the metadata essentially correspond to
        FITS-like parity, and we need to flip them to JPEG-like parity. See also
        very similar code in `astropix.py`.
        """
        headers = {}

        # headers['RADECSYS'] = self.wcs_coordinate_frame  # causes Astropy warnings
        headers["CTYPE1"] = "RA---" + self.metadata["Spatial.CoordsystemProjection"]
        headers["CTYPE2"] = "DEC--" + self.metadata["Spatial.CoordsystemProjection"]
        headers["CRVAL1"] = float(self.metadata["Spatial.ReferenceValue"][0])
        headers["CRVAL2"] = float(self.metadata["Spatial.ReferenceValue"][1])

        # See Calabretta & Greisen (2002; DOI:10.1051/0004-6361:20021327), eqn 186
        crot = np.cos(float(self.metadata["Spatial.Rotation"]) * np.pi / 180)
        srot = np.sin(float(self.metadata["Spatial.Rotation"]) * np.pi / 180)
        scale0 = float(self.metadata["Spatial.Scale"][0])

        # Seen in noao-02274; guessing how to handle this
        if not self.metadata["Spatial.Scale"][1]:
            scale1 = np.abs(scale0)
        else:
            scale1 = float(self.metadata["Spatial.Scale"][1])

        lam = scale1 / scale0

        pc1_1 = crot
        pc1_2 = -lam * srot
        pc2_1 = srot / lam
        pc2_2 = crot

        # If we couldn't get the original image, the pixel density used for
        # the WCS parameters may not match the image resolution that we have
        # available. In such cases, we need to remap the pixel-related
        # headers. From the available examples, `wcs_reference_pixel` seems to
        # be 1-based in the same way that `CRPIXn` are. Since in FITS, integer
        # pixel values correspond to the center of each pixel box, a CRPIXn of
        # [0.5, 0.5] (the lower-left corner) should not vary with the image
        # resolution. A CRPIXn of [W + 0.5, H + 0.5] (the upper-right corner)
        # should map to [W' + 0.5, H' + 0.5] (where the primed quantities are
        # the new width and height).

        factor0 = width / float(self.metadata["Spatial.ReferenceDimension"][0])
        factor1 = height / float(self.metadata["Spatial.ReferenceDimension"][1])

        headers["CRPIX1"] = (
            float(self.metadata["Spatial.ReferencePixel"][0]) - 0.5
        ) * factor0 + 0.5
        headers["CRPIX2"] = (
            float(self.metadata["Spatial.ReferencePixel"][1]) - 0.5
        ) * factor1 + 0.5

        # Now finalize and apply the parity flip.

        cdelt1 = scale0 / factor0
        cdelt2 = scale1 / factor1
        headers["CD1_1"] = cdelt1 * pc1_1
        headers["CD1_2"] = -cdelt1 * pc1_2
        headers["CD2_1"] = cdelt2 * pc2_1
        headers["CD2_2"] = -cdelt2 * pc2_2
        headers["CRPIX2"] = height + 1 - headers["CRPIX2"]

        return headers
