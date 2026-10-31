#! /usr/bin/env python
#
# Copyright 2022-2023 the .NET Foundation
# Licensed under the MIT License

"""
Tool for working with the WWT core dataset catalog.
"""

import argparse
from collections import OrderedDict
import csv
from io import BytesIO
import json
import math
import os.path
from pathlib import Path
import re
import shutil
import sys
import textwrap
import time
from typing import Dict, Optional, Tuple
import uuid
from xml.etree import ElementTree as etree
import yaml

from requests.exceptions import ConnectionError

from wwt_api_client import constellations as cx
from wwt_api_client.constellations.data import (
    SceneAstroPix,
    SceneContent,
    SceneImageLayer,
    ScenePlace,
)
from wwt_api_client.constellations.handles import HandleClient, AddSceneRequest

from wwt_data_formats import indent_xml
from wwt_data_formats.enums import (
    Bandpass,
    Classification,
    Constellation,
    DataSetType,
    FolderType,
    ProjectionType,
)
from wwt_data_formats.folder import Folder
from wwt_data_formats.imageset import ImageSet
from wwt_data_formats.place import Place

H2R = math.pi / 12
D2R = math.pi / 180
BASEDIR = Path(os.path.dirname(__file__))


def die(text, prefix="fatal error:", exitcode=1):
    print(prefix, text, file=sys.stderr)
    sys.exit(exitcode)


def warn(text):
    print("warning:", text, file=sys.stderr)


def write_multi_yaml(path, docs):
    with open(path, "wt", encoding="utf-8") as f:
        yaml.dump_all(
            docs,
            stream=f,
            allow_unicode=True,
            sort_keys=True,
            indent=2,
        )


def write_one_yaml(path, doc):
    with open(path, "wt", encoding="utf-8") as f:
        yaml.dump(
            doc,
            stream=f,
            allow_unicode=True,
            sort_keys=True,
            indent=2,
        )


# Imageset database


class ImagesetDatabase(object):
    db_dir: Path = None
    by_url: Dict[str, ImageSet] = None
    by_alturl: Dict[str, str] = None

    def __init__(self):
        self.by_url = {}
        self.by_alturl = {}
        self.db_dir = BASEDIR / "imagesets"

        for path in self.db_dir.glob("*.xml"):
            f = Folder.from_file(path)
            for c in f.children:
                assert isinstance(c, ImageSet)
                self.add_imageset(c)

    def add_imageset(
        self, imgset: ImageSet, queue_constellations_handle: Optional[str] = None
    ):
        if imgset.url in self.by_url:
            warn(f"dropping duplicated imageset `{imgset.url}`")
            return self.by_url[imgset.url]

        main_url = self.by_alturl.get(imgset.url)
        if main_url:
            warn(
                f"tried to add altUrl imageset `{imgset.url}`; use `{main_url}` instead"
            )
            return self.by_url[main_url]

        if queue_constellations_handle:
            if queue_constellations_handle == "skip":
                imgset.xmeta.cxstatus = "skip"
            else:
                imgset.xmeta.cxstatus = f"queue:{queue_constellations_handle}"

        if imgset.alt_url:
            main_url = self.by_alturl.get(imgset.alt_url)
            if main_url:
                warn(
                    f"duplicated AltUrl: {imgset.alt_url} => {main_url} AND {imgset.url}"
                )
            else:
                self.by_alturl[imgset.alt_url] = imgset.url

        self.by_url[imgset.url] = imgset
        return imgset

    def get_by_url(self, url: str) -> ImageSet:
        return self.by_url[url]

    def rewrite(self):
        by_key = {}

        for imgset in self.by_url.values():
            k = [str(imgset.data_set_type.value)]
            if imgset.reference_frame is not None and imgset.reference_frame != "Sky":
                k.append(imgset.reference_frame)
            if imgset.band_pass is not None:
                k.append(str(imgset.band_pass.value))

            key = "_".join(k).lower()
            by_key.setdefault(key, []).append(imgset)

        tempdir = Path(str(self.db_dir) + ".new")
        tempdir.mkdir()

        for key, imgsets in by_key.items():
            f = Folder(name=key)
            f.children = sorted(imgsets, key=lambda s: s.url)

            with (tempdir / (key + ".xml")).open("wt", encoding="utf-8") as stream:
                prettify(f.to_xml(), stream)

        olddir = Path(str(self.db_dir) + ".old")
        self.db_dir.rename(olddir)
        tempdir.rename(self.db_dir)
        shutil.rmtree(olddir)


# Place database


class PlaceDatabase(object):
    db_dir: Path = None
    by_uuid: Dict[str, dict] = None

    def __init__(self):
        self.by_uuid = {}
        self.db_dir = BASEDIR / "places"

        for path in self.db_dir.glob("*.yml"):
            with path.open("rt", encoding="utf-8") as f:
                for info in yaml.load_all(f, yaml.SafeLoader):
                    self.by_uuid[info["_uuid"]] = info

    def ingest_place(
        self,
        place: Place,
        idb: ImagesetDatabase,
        queue_constellations_handle: Optional[str] = None,
        new_id: Optional[str] = None,
    ):
        place.update_constellation()

        if place.image_set is not None:
            place.image_set = idb.add_imageset(
                place.image_set, queue_constellations_handle=queue_constellations_handle
            )
        if place.foreground_image_set is not None:
            place.foreground_image_set = idb.add_imageset(
                place.foreground_image_set,
                queue_constellations_handle=queue_constellations_handle,
            )
        if place.background_image_set is not None:
            place.background_image_set = idb.add_imageset(
                place.background_image_set,
                queue_constellations_handle=queue_constellations_handle,
            )

        if not new_id:
            new_id = str(uuid.uuid4())

        info = {"_uuid": new_id}

        if place.angle != 0:
            info["angle"] = place.angle

        if place.angular_size != 0:
            info["angular_size"] = place.angular_size

        if place.annotation:
            info["annotation"] = place.annotation

        if place.background_image_set:
            info["background_image_set_url"] = place.background_image_set.url

        if place.classification:
            info["classification"] = place.classification.value

        if place.constellation:
            info["constellation"] = place.constellation.value

        info["data_set_type"] = place.data_set_type.value

        if info["data_set_type"] == "Sky":
            info["ra_hr"] = place.ra_hr
            info["dec_deg"] = place.dec_deg
        else:
            info["latitude"] = place.latitude
            info["longitude"] = place.longitude

        if place.description:
            info["description"] = place.description

        if place.distance != 0:
            info["distance"] = place.distance

        if place.dome_alt != 0:
            info["dome_alt"] = place.dome_alt

        if place.dome_az != 0:
            info["dome_az"] = place.dome_az

        if place.foreground_image_set:
            info["foreground_image_set_url"] = place.foreground_image_set.url

        if place.image_set:
            info["image_set_url"] = place.image_set.url

        if place.magnitude != 0:
            info["magnitude"] = place.magnitude

        if place.msr_community_id != 0:
            info["msr_community_id"] = place.msr_community_id

        if place.msr_component_id != 0:
            info["msr_component_id"] = place.msr_component_id

        info["name"] = place.name

        if place.opacity != 100:
            info["opacity"] = place.opacity

        if place.permission != 0:
            info["permission"] = place.permission

        if place.rotation_deg != 0:
            info["rotation_deg"] = place.rotation_deg

        if place.thumbnail:
            info["thumbnail"] = place.thumbnail

        if place.zoom_level != 0:
            info["zoom_level"] = place.zoom_level

        if queue_constellations_handle == "skip":
            # If we're adding to a real handle, the appropriate course of
            # action will be detected from the imageset's cxstatus
            info["cxstatus"] = "skip"

        self.by_uuid[new_id] = info
        return new_id

    def reconst_by_id(self, pid: str, idb: ImagesetDatabase) -> Place:
        info = self.by_uuid[pid]

        place = Place()

        v = info.get("angle")
        if v is not None:
            place.angle = v

        v = info.get("angular_size")
        if v is not None:
            place.angular_size = v

        v = info.get("annotation")
        if v:
            place.annotation = v

        u = info.get("background_image_set_url")
        if u:
            place.background_image_set = idb.get_by_url(u)

        v = info.get("classification")
        if v:
            place.classification = Classification(v)

        v = info.get("constellation")
        if v:
            place.constellation = Constellation(v)

        place.data_set_type = DataSetType(info["data_set_type"])

        v = info.get("dec_deg")
        if v is not None:
            place.dec_deg = v

        v = info.get("description")
        if v:
            place.description = v

        v = info.get("distance")
        if v is not None:
            place.distance = v

        v = info.get("dome_alt")
        if v is not None:
            place.dome_alt = v

        v = info.get("dome_az")
        if v is not None:
            place.dome_az = v

        u = info.get("foreground_image_set_url")
        if u:
            try:
                place.foreground_image_set = idb.get_by_url(u)
            except KeyError:
                raise Exception(f"FG imageset URL `{u}` not found in place `{pid}`")

        u = info.get("image_set_url")
        if u:
            place.image_set = idb.get_by_url(u)

        v = info.get("latitude")
        if v is not None:
            place.latitude = v

        v = info.get("longitude")
        if v is not None:
            place.longitude = v

        v = info.get("magnitude")
        if v is not None:
            place.magnitude = v

        v = info.get("msr_community_id")
        if v is not None:
            place.msr_community_id = v

        v = info.get("msr_component_id")
        if v is not None:
            place.msr_component_id = v

        place.name = info["name"]

        v = info.get("opacity")
        if v is not None:
            place.opacity = v

        v = info.get("permission")
        if v is not None:
            place.permission = v

        v = info.get("ra_hr")
        if v is not None:
            place.ra_hr = v

        v = info.get("rotation_deg")
        if v is not None:
            place.rotation_deg = v

        v = info.get("thumbnail")
        if v:
            place.thumbnail = v

        v = info.get("zoom_level")
        if v is not None:
            place.zoom_level = v

        return place

    def rewrite(self):
        by_key = {}
        const_place = Place()

        for info in self.by_uuid.values():
            k = [info["data_set_type"]]

            ra = info.get("ra_hr")
            if ra is not None:
                ra = int(math.floor(ra)) % 24
                k.append(f"ra{ra:02d}")

                # Update constellation while we're at it
                const_place.set_ra_dec(info["ra_hr"], info["dec_deg"])
                info["constellation"] = const_place.constellation.value

            lon = info.get("longitude")
            if lon is not None:
                lon = (int(math.floor(lon)) // 10) * 10
                k.append(f"lon{lon:03d}")

            key = "_".join(k).lower()
            by_key.setdefault(key, []).append(info)

        def sortkey(info):
            k = []

            u = info.get("foreground_image_set_url")
            if u is not None:
                k += u

            u = info.get("image_set_url")
            if u is not None:
                k += u

            u = info.get("background_image_set_url")
            if u is not None:
                k += u

            dec = info.get("dec_deg")
            if dec is not None:
                k += [dec, info["ra_hr"]]

            lat = info.get("latitude")
            if lat is not None:
                k.append(lat)
                lon = info.get("longitude")  # there is a busted dataset like this
                if lon is not None:
                    k.append(lon)

            k += [info["name"]]
            return tuple(k)

        tempdir = Path(str(self.db_dir) + ".new")
        tempdir.mkdir()

        for key, infos in by_key.items():
            infos = sorted(infos, key=sortkey)
            write_multi_yaml(tempdir / (key + ".yml"), infos)

        olddir = Path(str(self.db_dir) + ".old")
        self.db_dir.rename(olddir)
        tempdir.rename(self.db_dir)
        shutil.rmtree(olddir)


# Constellations prep database


def _parse_record_file(stream, path):
    kind = None
    fields = OrderedDict()
    multiline_key = None
    multiline_words = []
    line_num = 0

    for line in stream:
        line_num += 1
        line = line.strip()
        if not line:
            continue

        if kind is None:
            if line.startswith("@"):
                kind = line[1:].split()[0]
            else:
                die(
                    f"expected @ indicator at line {line_num} of `{path}`; got: {line!r}"
                )
        elif line == "---":
            if multiline_key:
                fields[multiline_key] = " ".join(multiline_words)
                multiline_key = None
                multiline_words = []

            yield kind, fields

            kind = None
            fields = OrderedDict()
        else:
            pieces = line.split()

            if pieces[0].endswith(":"):
                if multiline_key:
                    fields[multiline_key] = " ".join(multiline_words)
                    multiline_key = None
                    multiline_words = []

                fields[pieces[0][:-1]] = " ".join(pieces[1:])
            elif pieces[0].endswith(">"):
                if multiline_key:
                    fields[multiline_key] = " ".join(multiline_words)
                    multiline_key = None
                    multiline_words = []

                multiline_key = pieces[0][:-1]
                multiline_words = pieces[1:]
            elif multiline_key:
                multiline_words += pieces
            else:
                die(
                    f"expected : or > indicator at line {line_num} of `{path}`; got: {line!r}"
                )

    if kind or fields or multiline_key:
        die(f"file `{path}` must end with an end-of-record indicator (---)")


def _emit_record(kind, fields, stream):
    print(f"\n@{kind}", file=stream)
    prev_had_blank_line = False

    for key, value in fields.items():
        if key in ("text", "credits"):
            if not prev_had_blank_line:
                print(file=stream)

            wrapped = textwrap.wrap(
                f"{key}> {value}",
                width=80,
                break_long_words=False,
                break_on_hyphens=False,
            )

            # Avoid emitting a continuation line whose first token ends in ":"
            # or ">". `_parse_record_file` would misinterpret such a line as the
            # start of a new field (e.g. text containing "[Image description:
            # ...]"). Merge any offending leading token onto the previous line.
            safe_lines = []
            for line in wrapped:
                if safe_lines:
                    first_tok = line.split(None, 1)[0]
                    if first_tok.endswith((":", ">")):
                        safe_lines[-1] = safe_lines[-1] + " " + line
                        continue
                safe_lines.append(line)

            for line in safe_lines:
                print(line, file=stream)

            print(file=stream)
            prev_had_blank_line = True
        else:
            print(f"{key}: {value}", file=stream)
            prev_had_blank_line = False

    print("---", file=stream)


def _retry(operation):
    """
    My computer will sometimes fail during large bootstraps due to temporary,
    local network errors. Here's a dumb retry system since the design of the
    openidc_client library that underlies wwt_api_client doesn't allow me to
    activate retries at the request/urllib3 level, as far as I can see.
    """
    for _attempt in range(5):
        try:
            return operation()
        except ConnectionError:
            print("(retrying ...)")
            time.sleep(0.5)


def _register_image(
    client: HandleClient, fields: dict, imgset: ImageSet, dry_run: bool = False
) -> str:
    "Returns the new image ID"

    if imgset.band_pass != Bandpass.VISIBLE:
        print(
            f"warning: imageset `{imgset.name}` has non-default band_pass setting `{imgset.band_pass}`"
        )
    if imgset.base_tile_level != 0:
        print(
            f"warning: imageset `{imgset.name}` has non-default base_tile_level setting `{imgset.base_tile_level}`"
        )
    if imgset.data_set_type != DataSetType.SKY:
        print(
            f"warning: imageset `{imgset.name}` has non-default data_set_type setting `{imgset.data_set_type}`"
        )
    if imgset.elevation_model != False:
        print(
            f"warning: imageset `{imgset.name}` has non-default elevation_model setting `{imgset.elevation_model}`"
        )
    if imgset.generic != False:
        print(
            f"warning: imageset `{imgset.name}` has non-default generic setting `{imgset.generic}`"
        )
    if imgset.sparse != True:
        print(
            f"warning: imageset `{imgset.name}` has non-default sparse setting `{imgset.sparse}`"
        )
    if imgset.stock_set != False:
        print(
            f"warning: imageset `{imgset.name}` has non-default stock_set setting `{imgset.stock_set}`"
        )

    credits = fields["credits"]
    copyright = fields["copyright"]
    license_id = fields["license_id"]
    alt_text = fields.get("description")

    if dry_run:
        print("*not* registering image:", imgset.url, "=>", end=" ")
        id = "<fakeimageid>"
    else:
        print("registering image:", imgset.url, "=>", end=" ")
        id = _retry(
            lambda: client.add_image_from_set(
                imgset,
                copyright,
                license_id,
                credits=credits,
                alt_text=alt_text,
            )
        )

    print(id)
    return id


def _register_scene(
    client: HandleClient,
    fields: dict,
    place: Place,
    imgid: str,
    dry_run: bool = False,
    apid: Optional[str] = None,
    published: bool = True,
) -> str:
    "Returns the new scene ID"

    image_layers = [SceneImageLayer(image_id=imgid, opacity=1.0)]

    api_place = ScenePlace(
        ra_rad=place.ra_hr * H2R,
        dec_rad=place.dec_deg * D2R,
        roll_rad=place.rotation_deg * D2R,
        roi_height_deg=place.zoom_level / 6,
        roi_aspect_ratio=1.0,
    )

    content = SceneContent(image_layers=image_layers)

    req = AddSceneRequest(
        place=api_place,
        content=content,
        text=fields["text"],
        outgoing_url=fields["outgoing_url"],
        published=published,
    )

    if apid:
        publisher_id, image_id = apid.split("|")
        req.astropix = SceneAstroPix(
            publisher_id=publisher_id,
            image_id=image_id,
        )

    if dry_run:
        print("*not* registering place/scene:", fields["place_uuid"], "=>", end=" ")
        id = "<fakesceneid>"
    else:
        print("registering place/scene:", fields["place_uuid"], "=>", end=" ")
        id = _retry(lambda: client.add_scene(req))

    print(id)
    return id


class ConstellationsPrepDatabase(object):
    db_dir: Path = None
    by_handle: Dict[str, list] = None

    def __init__(self):
        self.by_handle = {}
        self.db_dir = BASEDIR / "cxprep"

        for path in self.db_dir.glob("*.txt"):
            with path.open("rt", encoding="utf-8") as f:
                handle = path.name.replace(".txt", "")
                items = list(_parse_record_file(f, path))
                self.by_handle[handle] = items

    def update(self, idb: ImagesetDatabase, pdb: PlaceDatabase):
        # Figure out which imagesets are already "done": they are either
        # logged as "in" in the XML, or already in one of the prep files

        done_imageset_urls = set()

        for url, imgset in idb.by_url.items():
            cxs = getattr(imgset.xmeta, "cxstatus", "undefined")
            if cxs.startswith("in:"):
                done_imageset_urls.add(url)

        for recs in self.by_handle.values():
            for kind, fields in recs:
                if kind == "image":
                    done_imageset_urls.add(fields["url"])

        # Same idea for places/scenes

        done_place_uuids = set()

        for uuid, pinfo in pdb.by_uuid.items():
            cxs = pinfo.get("cxstatus", "undefined")
            if cxs.startswith("in:"):
                done_place_uuids.add(uuid)

        for recs in self.by_handle.values():
            for kind, fields in recs:
                if kind == "scene":
                    done_place_uuids.add(fields["place_uuid"])

        # Build up a list of imagesets to deal with

        todo_image_urls_by_handle = {}
        n_images_todo = 0

        for url, imgset in idb.by_url.items():
            if imgset.data_set_type != DataSetType.SKY:
                continue

            if url in done_imageset_urls:
                continue

            cxs = getattr(imgset.xmeta, "cxstatus", "undefined")
            if cxs.startswith("in:") or cxs == "skip":
                continue

            if not cxs.startswith("queue:"):
                # can't handle this since we don't know what handle to
                # associate it with
                warn(
                    f"imageset {url} should have Constellations ingest status flag, but doesn't"
                )
                continue

            handle = cxs[6:]
            todo_image_urls_by_handle.setdefault(handle, set()).add(url)
            n_images_todo += 1

        print(f"Number of imagesets to append:", n_images_todo)

        # Build up a list of places/scenes to deal with. Also associate them
        # with imageset URLs so that we can emit todo places next to todo images
        # -- it makes a big difference to do so in practice, because when we're
        # adapting info into the Constellations schema, the relevant metadata
        # gets slightly split between images and scenes.

        todo_place_uuids_by_handle = {}
        n_places_todo = 0
        pids_by_image_url = {}

        for uuid, pinfo in pdb.by_uuid.items():
            cxs = pinfo.get("cxstatus", "undefined")
            if cxs.startswith("in:") or cxs == "skip":
                continue

            handle = None
            matched_url = None

            for k in [
                "image_set_url",
                "foreground_image_set_url",
                "background_image_set_url",
            ]:
                url = pinfo.get(k)
                if not url:
                    continue

                for h, urls in todo_image_urls_by_handle.items():
                    if url in urls:
                        # Aha! This place is associated with a to-do image, under
                        # the specified handle

                        if handle is None:
                            handle = h
                            matched_url = url
                        elif h != handle:
                            warn(
                                f"place {uuid} matches images from multiple handles: {h}, {handle}"
                            )

                        pids_by_image_url.setdefault(url, set()).add(uuid)

            if handle is None:
                # This place does not refer to any images that we plan to add to
                # Constellations. So we can ignore it.
                continue

            todo_place_uuids_by_handle.setdefault(handle, {})[uuid] = matched_url
            n_places_todo += 1

        print(f"Number of places/scenes to append:", n_places_todo)

        # Now we can finally actually add the new items to the lists. For each
        # image, we add any places associated with it right after, to achieve
        # the aformentioned desired clustering. Those places are then removed
        # from the todo list. Then, after doing all the imageses, we mop up any
        # places that may be hanging around.

        for handle, imgurls in todo_image_urls_by_handle.items():
            items = self.by_handle.setdefault(handle, [])
            todo_places = todo_place_uuids_by_handle.get(handle, {})

            for url in sorted(imgurls):
                imgset = idb.by_url[url]
                fields = OrderedDict()
                fields["url"] = url
                fields["copyright"] = "~~COPYRIGHT~~"
                fields["license_id"] = "~~LICENSE~~"
                if imgset.thumbnail_url:
                    fields["thumbnail"] = imgset.thumbnail_url

                apids = getattr(imgset.xmeta, "astropix_ids", None)
                if apids:
                    fields["astropix_ids"] = apids

                fields["credits"] = imgset.credits
                fields["wip"] = "yes"
                items.append(("image", fields))

                for pid in pids_by_image_url.get(url, []):
                    if pid not in todo_places:
                        # Looks like this place was already emitted elsewhere.
                        # This won't happen in typical usage, but is OK.
                        continue

                    fields = OrderedDict()
                    fields["place_uuid"] = pid
                    fields["image_url"] = url
                    fields["outgoing_url"] = imgset.credits_url

                    pinfo = pdb.by_uuid[pid]

                    if pinfo.get("thumbnail"):
                        fields["thumbnail"] = pinfo["thumbnail"]

                    text = pinfo.get("description")

                    if not text:
                        text = imgset.description

                    if not text:
                        text = pinfo["name"]

                    fields["text"] = text
                    fields["wip"] = "yes"
                    items.append(("scene", fields))
                    del todo_places[pid]

        for handle, pids in todo_place_uuids_by_handle.items():
            items = self.by_handle.setdefault(handle, [])

            for pid, matched_url in sorted(pids.items()):
                imgset = idb.by_url[matched_url]

                fields = OrderedDict()
                fields["place_uuid"] = pid
                fields["image_url"] = matched_url
                fields["outgoing_url"] = imgset.credits_url

                pinfo = pdb.by_uuid[pid]
                text = pinfo.get("description")

                if not text:
                    text = imgset.description

                if not text:
                    text = pinfo["name"]

                fields["text"] = text
                fields["wip"] = "yes"
                items.append(("scene", fields))

        # All done! Call rewrite() after this if you don't want to lose all this work.

    def register(
        self,
        client: cx.CxClient,
        idb: ImagesetDatabase,
        pdb: PlaceDatabase,
        dry_run: bool = False,
    ):
        # prefill the tables of image info by URL since we may need
        # these to construct scene records, if we create the image in one
        # session and an associated scene in another.

        imgids_by_url = {}
        apids_by_url = {}

        for url, imgset in idb.by_url.items():
            cxs = getattr(imgset.xmeta, "cxstatus", "")
            if cxs.startswith("in:"):
                imgids_by_url[url] = cxs[3:]

            apids = getattr(imgset.xmeta, "astropix_ids", "")
            if apids:
                apids_by_url[url] = apids

        # now we can actually register the new stuff

        n = 0
        done_apids = set()

        for handle in list(self.by_handle.keys()):
            items = self.by_handle[handle]
            remove_img_urls = set()
            remove_place_uuids = set()
            handle_client = None

            try:
                for kind, fields in items:
                    if "wip" in fields:
                        # Not yet ready
                        continue

                    if "skip" in fields:
                        # Something to skip. We still need to do a little work here
                        # to log this decision in the main database files.

                        if kind == "image":
                            url = fields["url"]
                            idb.by_url[url].xmeta.cxstatus = f"skip"
                            remove_img_urls.add(url)
                        elif kind == "scene":
                            uuid = fields["place_uuid"]
                            pdb.by_uuid[uuid]["cxstatus"] = f"skip"
                            remove_place_uuids.add(uuid)
                        else:
                            warn(f"unexpected skipped prep item kind `{kind}`")

                        continue

                    # Ooh, we have something to upload!
                    if handle_client is None:
                        handle_client = client.handle_client(handle)

                    if kind == "image":
                        url = fields["url"]
                        imgset = idb.by_url[url]

                        apids = fields.get("astropix_ids")
                        if apids:
                            apids_by_url[url] = apids

                        id = _register_image(
                            handle_client, fields, imgset, dry_run=dry_run
                        )
                        imgset.xmeta.cxstatus = f"in:{id}"
                        imgids_by_url[url] = id
                        remove_img_urls.add(url)
                        n += 1
                    elif kind == "scene":
                        uuid = fields["place_uuid"]
                        img_url = fields["image_url"]
                        img_id = imgids_by_url.get(img_url)

                        if not img_id:
                            warn(
                                f"can't register place/scene {uuid} because can't determine CXID for imageset {img_url}"
                            )
                            continue

                        apids = apids_by_url.get(img_url)
                        apid = None

                        if apids:
                            apids = apids.split(",")
                            if len(apids) > 1:
                                warn(
                                    f"place/scene {uuid} associated with multiple AstroPix IDs via imageset {img_url}; only recording the first"
                                )

                            apid = apids[0]

                            if apid in done_apids:
                                # This check only knows about what we've added
                                # within this one session, so it is far from
                                # foolproof, but hopefully it can catch the
                                # common case of two scenes associated with the
                                # same image.
                                warn(
                                    f"place/scene {uuid} associated with AstroPix {apid} via imageset {img_url}, but it was already registered this session"
                                )
                                apid = None
                            else:
                                done_apids.add(apid)

                        place = pdb.reconst_by_id(uuid, idb)
                        id = _register_scene(
                            handle_client,
                            fields,
                            place,
                            img_id,
                            dry_run=dry_run,
                            apid=apid,
                        )
                        pdb.by_uuid[uuid]["cxstatus"] = f"in:{id}"
                        pdb.by_uuid[uuid]["astropix_id"] = apid
                        remove_place_uuids.add(uuid)
                        n += 1
                    else:
                        warn(f"unexpected prep item kind `{kind}`")
            finally:
                # Update the list of items. We take this particular approach so
                # that if we crash mid-operation, progress will be correctly
                # saved, to the best of our ability.

                new_items = []

                for kind, fields in items:
                    if kind == "image" and fields["url"] in remove_img_urls:
                        continue

                    if kind == "scene" and fields["place_uuid"] in remove_place_uuids:
                        continue

                    new_items.append((kind, fields))

                self.by_handle[handle] = new_items

        # All done! Rewrite this and the idb and the pdb afterwards
        return n

    def rewrite(self):
        for handle, items in self.by_handle.items():
            path = self.db_dir / f"{handle}.txt"

            with path.open("wt", encoding="utf-8") as f:
                for kind, fields in items:
                    _emit_record(kind, fields, f)


# add-alt-urls


def do_add_alt_urls(settings):
    """
    Implemented to migrate the Mars panorama URLs, which differ in `imagesets6`
    and the Mars Explore database.
    """
    idb = ImagesetDatabase()

    with open(settings.spec_path, "rt") as f:
        for line in f:
            old_url, new_url = line.strip().split()

            imgset = idb.by_url.get(new_url)
            if imgset is None:
                die(f"missing new-url `{new_url}`")

            if imgset.alt_url and imgset.alt_url != old_url:
                die(f"preexisting AltUrl `{imgset.alt_url}` for `{new_url}`")

            imgset.alt_url = old_url

    idb.rewrite()


# emit


def _emit_one(path: Path, is_preview: bool, idb: ImagesetDatabase, pdb: PlaceDatabase):
    with path.open("rt", encoding="utf-8") as f:
        root_info = yaml.load(f, yaml.SafeLoader)

    def reconst_folder(info: dict):
        f = Folder()
        f.browseable = info["browseable"]

        v = info.get("group")
        if v:
            f.group = v

        v = info.get("msr_community_id")
        if v:
            f.msr_community_id = v

        v = info.get("msr_component_id")
        if v:
            f.msr_component_id = v

        f.name = info["name"]

        v = info.get("permission")
        if v:
            f.permission = v

        f.searchable = info["searchable"]

        v = info.get("sub_type")
        if v:
            f.sub_type = v

        v = info.get("thumbnail")
        if v:
            f.thumbnail = v

        v = info.get("type")
        if v:
            f.type = FolderType(v)

        v = info.get("url")
        if v:
            if is_preview and v.startswith(
                "http://www.worldwidetelescope.org/wwtweb/catalog.aspx?W="
            ):
                catname = v.split("=")[1]
                catpath = BASEDIR / "catfiles" / f"{catname}.yml"

                if catpath.exists():
                    v = f"./{catname}.wtml"

            f.url = v

        f.children = []

        for spec in info["children"]:
            if isinstance(spec, str):
                if spec.startswith("imageset "):
                    f.children.append(idb.get_by_url(spec[9:]))
                elif spec.startswith("place "):
                    f.children.append(pdb.reconst_by_id(spec[6:], idb))
                else:
                    assert False, f"unexpected terse folder child `{spec}`"
            else:
                f.children.append(reconst_folder(spec))

        return f

    f = reconst_folder(root_info)
    catname = os.path.splitext(os.path.basename(path))[0]
    rel = "_rel" if is_preview else ""
    extension = "xml" if root_info.get("_is_xml", False) else "wtml"

    with open(f"{catname}{rel}.{extension}", "wt", encoding="utf-8") as stream:
        prettify(f.to_xml(), stream)
        print(f"wrote `{catname}{rel}.{extension}`")


def do_emit(settings):
    idb = ImagesetDatabase()
    pdb = PlaceDatabase()

    for path in (BASEDIR / "catfiles").glob("*.yml"):
        _emit_one(path, settings.preview, idb, pdb)


# emit-partition


def do_emit_partition(settings):
    idb = ImagesetDatabase()
    pdb = PlaceDatabase()

    # Load the partition database

    imageset_urls = set()

    with open(settings.partition_path, "rt") as f:
        for line in f:
            pieces = line.strip().split(None, 2)
            url, name = pieces[:2]

            if name == settings.partition_name:
                imageset_urls.add(url)

    print(
        f"Loaded {len(imageset_urls)} imageset URLs associated with partition `{settings.partition_name}`",
    )
    if not imageset_urls:
        return

    # Populate places

    f = Folder()
    n_places = 0
    emitted_img_urls = set()

    for uuid, p in pdb.by_uuid.items():
        img_url = p.get("foreground_image_set_url")

        if img_url is None or img_url not in imageset_urls:
            img_url = p.get("image_set_url")

            if img_url is None or img_url not in imageset_urls:
                continue

        f.children.append(pdb.reconst_by_id(uuid, idb))
        emitted_img_urls.add(img_url)
        n_places += 1

    print(f"Matched {n_places} associated places")

    # Clean up any additional imagesets

    cleanup_urls = imageset_urls - emitted_img_urls

    for img_url in cleanup_urls:
        # hack: my partition file has a bunch of URLs that aren't in the database
        # because I had to cull/correct a bunch of Spitzer imagesets
        if img_url not in idb.by_url:
            continue

        imgset = idb.get_by_url(img_url)
        f.children.append(imgset)

    print(f"Added {len(cleanup_urls)} cleanup image definitions")

    # Write

    with open(settings.wtml_path, "wt", encoding="utf-8") as stream:
        prettify(f.to_xml(), stream)


# emit-searchdata


def _parse_classification(text):
    text = text.replace(" ", "")
    if text == "OpenStarCluster":
        return Classification.OPEN_CLUSTER
    if text == "TripleStar":
        return Classification.MULTIPLE_STARS
    return Classification(text)


def _compute_constellation(ra_deg, dec_deg):
    pl = Place()
    pl.set_ra_dec(ra_deg / 15, dec_deg)
    return pl.constellation


def _scan_cat_file(settings, name, need_constellation=False):
    with open(os.path.join(settings.catdir, name + ".txt")) as f:
        for line in f:
            bits = line.rstrip().split("\t")
            info = {}
            info["n"] = bits[0]  # name
            info["c"] = _parse_classification(bits[1]).to_numeric()
            info["r_deg"] = float(bits[2])
            info["d_deg"] = float(bits[3])

            if info["r_deg"] == 0.0 and info["d_deg"] == 0.0 and name != "ssobjects":
                warn(
                    f"suspicious catalog object RA = Dec = 0: `{info['n']}` in `{name}`"
                )
                warn("did you apply the patch? Search README for `repair-catalogs`")

            if len(bits) > 4:
                if len(bits[4]) and bits[4] != "NULL":
                    info["m"] = float(bits[4])  # magnitude

            # bits[5] is the constellation, but in the Messier and NGC catalogs
            # it is often totally incorrect. In other cases, some objects are
            # really right at the borders (e.g., IC2036, IC3031) and WWT's
            # algorithm yields a different answer than some traditional
            # classifications. Either way, it works best to always rederive the
            # constellation.

            if need_constellation:
                info["constellation"] = _compute_constellation(
                    info["r_deg"], info["d_deg"]
                )

            if len(bits) > 6:
                info["z"] = float(bits[6])  # zoom

            yield info


def do_emit_searchdata(settings):
    # First prep hash by constellation:

    by_const = {}

    def _keys():
        for c in Constellation:
            if c != Constellation.UNSPECIFIED:
                yield c.value
        yield "SolarSystem"
        yield "Constellations"

    for k in _keys():
        by_const[k] = []

    # Populate places/imagesets, keeping track of some stats to inform our
    # compression tactics.

    idb = ImagesetDatabase()
    pdb = PlaceDatabase()
    n = 0

    n_bp = {}
    n_lv = {}
    n_q = {}
    n_c = {}
    n_ft = {}
    n_ox_hits = 0
    n_oy_hits = 0

    def incr(tbl, key):
        tbl[key] = tbl.get(key, 0) + 1

    for pid in pdb.by_uuid.keys():
        pl = pdb.reconst_by_id(pid, idb)

        if pl.data_set_type != DataSetType.SKY:
            continue

        img = pl.foreground_image_set
        if img is None:
            continue

        # Note: excluding Healpix, SkyImage, etc.
        if img.projection != ProjectionType.TAN:
            continue

        fgi = {
            "bd": img.base_degrees_per_tile,
            "cX": img.center_x,
            "cY": img.center_y,
            "ct": img.credits,
            "cu": img.credits_url,
            "n": img.name,
            "tu": img.thumbnail_url,
            "u": img.url,
            "wf": img.width_factor,
        }

        if img.base_tile_level != 0:
            fgi["bl"] = 0
        if img.band_pass != Bandpass.VISIBLE:
            fgi["bp"] = img.band_pass.value
        if img.bottoms_up:
            fgi["bu"] = img.bottoms_up
        if img.tile_levels != 4:
            fgi["lv"] = img.tile_levels
        if img.offset_x != 0:
            fgi["oX"] = img.offset_x
        else:
            n_ox_hits += 1
        if img.offset_y != 0:
            fgi["oY"] = img.offset_y
        else:
            n_oy_hits += 1
        if img.stock_set:
            fgi["ds"] = img.stock_set
        if img.quad_tree_map:
            fgi["q"] = img.quad_tree_map
        if img.rotation_deg != 0:
            fgi["r"] = img.rotation_deg
        if img.width_factor != 2:
            fgi["wf"] = img.width_factor
        if img.file_type != ".png":
            fgi["ft"] = img.file_type

        # not even worrying about "dt" = data_set_type: always Sky
        # ditto for "pr" = projection: always Tan

        # TODO: clean up classifications in database
        c = pl.classification
        if c == Classification.UNSPECIFIED:
            c = Classification.UNIDENTIFIED

        # classification groups erroneously used on
        # individual images:

        if c == Classification.STELLAR_GROUPINGS:
            c = Classification.MULTIPLE_STARS
        if c == Classification.UNFILTERED:
            c = Classification.UNIDENTIFIED
        if c == Classification.GALACTIC:
            c = Classification.GALAXY
        if c == Classification.STELLAR:
            c = Classification.STAR
        if c == Classification.OTHER:
            c = Classification.UNIDENTIFIED

        info = {
            "d_deg": pl.dec_deg,
            "fgi": fgi,
            "n": pl.name,
            "r_deg": pl.ra_hr * 15,  # so that we can homogeneously convert below
        }

        if c != Classification.UNIDENTIFIED:
            info["c"] = c.to_numeric()
        if pl.zoom_level != -1:
            info["z"] = pl.zoom_level

        by_const[pl.constellation.value].append(info)
        n += 1

        # other stats

        incr(n_bp, img.band_pass)
        incr(n_lv, img.tile_levels)
        incr(n_q, img.quad_tree_map)
        incr(n_c, c)
        incr(n_ft, img.file_type)

    print(f"note: declared {n} imagesets", file=sys.stderr)

    def report(tbl, desc):
        key, count = max(tbl.items(), key=lambda t: t[1])
        print(f"note: most common {desc} value: `{key}` ({count})", file=sys.stderr)

    report(n_bp, "bandpass")
    report(n_lv, "tile_levels")
    report(n_q, "quad_tree_map")
    report(n_c, "classification")
    report(n_ft, "file_type")
    print(f"note: was able to optimize out offset_x {n_ox_hits} times", file=sys.stderr)
    print(f"note: was able to optimize out offset_y {n_oy_hits} times", file=sys.stderr)

    # Populate key catalogs

    n = 0

    for cat in ("messier", "ngc", "ic", "commonstars", "bsc"):
        for info in _scan_cat_file(settings, cat, need_constellation=True):
            place_list = by_const[info["constellation"].value]
            del info["constellation"]
            place_list.append(info)
            n += 1

    print(f"note: declared {n} common catalog items", file=sys.stderr)

    # Special solar-system section, with hack to add Earth.

    place_list = by_const["SolarSystem"]
    for info in _scan_cat_file(settings, "ssobjects"):
        place_list.append(info)

        if info["n"] == "Venus":
            earth = dict(info)
            earth["n"] = "Earth"
            place_list.append(earth)

    # Special Constellations section.

    place_list = by_const["Constellations"]
    for info in _scan_cat_file(settings, "constellationlist"):
        place_list.append(info)

    # Transform into final structure

    for place_list in by_const.values():
        for pl in place_list:
            ra_deg = pl.pop("r_deg")
            pl["r"] = round(ra_deg / 15, 4)  # convert to hours!

            dec_deg = pl.pop("d_deg")
            pl["d"] = round(dec_deg, 4)

            zoom = pl.get("z")
            if zoom is not None:
                pl["z"] = round(zoom, 5)
            else:
                pl["z"] = -1

            # Magnitude data unused in webclient
            pl.pop("m", None)

    wrapper = {"Constellations": [{"name": k, "places": by_const[k]} for k in _keys()]}

    if settings.pretty_json:
        import json

        json.dump(wrapper, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    else:
        import json5

        print("wwt.searchData=", end="")
        json5.dump(
            wrapper,
            sys.stdout,
            ensure_ascii=False,
            check_circular=False,
            allow_nan=False,
            trailing_commas=False,
            allow_duplicate_keys=False,
            separators=(",", ":"),
        )
        print(";")


# forget


def do_forget(settings):
    # Remove the imageset definitions

    idb = ImagesetDatabase()

    for imgurl in settings.image_urls:
        try:
            del idb.by_url[imgurl]
        except KeyError:
            warn(f"no imageset with URL {imgurl}")

    # Remove places that use the imageset(s)

    pdb = PlaceDatabase()
    image_urls = frozenset(settings.image_urls)
    removed_pids = set()

    for pid in list(pdb.by_uuid.keys()):
        pinfo = pdb.by_uuid[pid]

        for key in (
            "image_set_url",
            "background_image_set_url",
            "foreground_image_set_url",
        ):
            url = pinfo.get(key)

            if url is not None and url in image_urls:
                break
        else:
            # loop did not exit early - no match
            continue

        del pdb.by_uuid[pid]
        removed_pids.add(pid)

    # Remove folder entries that use the places

    def folder_child_filter(spec):
        if isinstance(spec, str):
            if spec.startswith("imageset "):
                url = spec[9:]
                if url in image_urls:
                    return False
            elif spec.startswith("place "):
                pid = spec[6:]
                if pid in removed_pids:
                    return False

        return True

    for path in (BASEDIR / "catfiles").glob("*.yml"):
        with path.open("rt", encoding="utf-8") as f:
            root_info = yaml.load(f, yaml.SafeLoader)

        def filter_folder(info: dict) -> Tuple[dict, bool]:
            """Take care to keep track of whether we modified anything,
            since automated rewrites of the YAML discard comments, leading to
            excessive Git diffs."""

            n_before = len(info["children"])
            new_children = list(filter(folder_child_filter, info["children"]))
            modified = len(new_children) != n_before

            for i in range(len(new_children)):
                spec = new_children[i]

                if not isinstance(spec, str):
                    new_children[i], child_modified = filter_folder(spec)
                    modified = modified or child_modified

            info["children"] = new_children
            return info, modified

        root_info, modified = filter_folder(root_info)

        if modified:
            write_one_yaml(path, root_info)

    # Rewrite the other bits

    idb.rewrite()
    pdb.rewrite()


# format-imagesets


def do_format_imagesets(_settings):
    idb = ImagesetDatabase()
    idb.rewrite()


# format-places


def do_format_places(_settings):
    pdb = PlaceDatabase()
    pdb.rewrite()


# ground-truth


def do_ground_truth(_settings):
    import requests

    for path in (BASEDIR / "catfiles").glob("*.yml"):
        with path.open("rt", encoding="utf-8") as f:
            info = yaml.load(f, yaml.SafeLoader)

        catname = os.path.splitext(os.path.basename(path))[0]

        if info.get("_is_xml", False):
            extension = "xml"
            letter = "X"
        else:
            extension = "wtml"
            letter = "W"

        url = (
            f"http://www.worldwidetelescope.org/wwtweb/catalog.aspx?{letter}={catname}"
        )
        filename = f"{catname}.{extension}"

        with requests.get(url, stream=True) as r:
            with open(filename, "wb") as f:
                shutil.copyfileobj(r.raw, f)

            print(f"wrote `{filename}`")


# ingest


def do_ingest(settings):
    catname = os.path.splitext(os.path.basename(settings.wtml))[0]
    f = Folder.from_file(settings.wtml)
    idb = ImagesetDatabase()
    pdb = PlaceDatabase()

    if settings.cx_handle != "skip":
        if not os.path.exists(f"cxprep/{settings.cx_handle}.txt"):
            die(
                f"expected file `cxprep/{settings.cx_handle}.txt` to exist but it's missing - mistaken handle name?"
            )

    def folder_to_yaml(f):
        info = {}
        info["browseable"] = f.browseable

        if f.group:
            info["group"] = f.group

        if f.msr_community_id:
            info["msr_community_id"] = f.msr_community_id

        if f.msr_component_id:
            info["msr_component_id"] = f.msr_component_id

        info["name"] = f.name

        if f.permission:
            info["permission"] = f.permission

        info["searchable"] = f.searchable

        if f.sub_type:
            info["sub_type"] = f.sub_type

        if f.thumbnail:
            info["thumbnail"] = f.thumbnail

        info["type"] = f.type.value

        if f.url:
            info["url"] = f.url

        children = []

        for c in f.children:
            if isinstance(c, ImageSet):
                c = idb.add_imageset(c, queue_constellations_handle=settings.cx_handle)
                children.append(f"imageset {c.url}")
            elif isinstance(c, Place):
                pid = pdb.ingest_place(
                    c, idb, queue_constellations_handle=settings.cx_handle
                )
                children.append(f"place {pid}")
            elif isinstance(c, Folder):
                if not c.children and c.url:
                    print(f"Consider ingesting: {c.url}")
                children.append(folder_to_yaml(c))
            else:
                assert False, "unexpected folder item??"

        info["children"] = children
        return info

    info = folder_to_yaml(f)
    idb.rewrite()
    pdb.rewrite()

    if settings.emit:
        write_one_yaml(f"catfiles/{catname}.yml", info)

    if settings.prepend_to:
        print(f"Updating `{settings.prepend_to}`.")

        with open(settings.prepend_to, "rt", encoding="utf-8") as f:
            existing = yaml.load(f, yaml.SafeLoader)

        existing["children"] = info["children"] + existing["children"]
        write_one_yaml(settings.prepend_to, existing)

    print()
    print(
        "Consider running `cattool.py update-cxprep` to update the Constellations-prep data files"
    )


# partition - assist with partitioning the imagesets into topical categories


def do_partition(settings):
    idb = ImagesetDatabase()

    # Load up the existing file

    partitioned = {}

    with open(settings.path, "rt") as f:
        for line in f:
            pieces = line.strip().split(None, 2)
            url, tag = pieces[:2]

            if len(pieces) > 2:
                rest = pieces[2]
            else:
                rest = ""

            partitioned[url] = (tag, rest)

    # Load up the database and fill in anything missing

    n_new = 0

    for url, imgset in idb.by_url.items():
        if imgset.data_set_type != DataSetType.SKY:
            continue
        if not url.strip():
            continue
        if "tdf" in imgset.file_type:
            continue  # tiled catalog

        tup = partitioned.get(url)

        if tup is not None:
            tag, desc = tup
        else:
            n_new += 1
            tag = "UNASSIGNED"
            desc = ""

        if not desc:
            desc = f"{imgset.name} / {imgset.credits_url}"

        partitioned[url] = (tag, desc)

    # Summarize

    counts = {}
    n_total = 0

    for url, (tag, _name) in partitioned.items():
        counts[tag] = counts.get(tag, 0) + 1
        n_total += 1

    print("Categories:\n")

    for tag, count in sorted(counts.items(), key=lambda t: t[1]):
        print(f"{tag:16} {count:4}")

    print(f"\nTotal: {n_total}")
    print(f"Added {n_new} new imagesets")

    # Rewrite in canonical format

    with open(settings.path, "wt") as f:
        for url, (tag, name) in sorted(partitioned.items()):
            if name:
                print(f"{url}  {tag}  {name}", file=f)
            else:
                print(f"{url}  {tag}", file=f)


# prettify - generic XML prettification


def prettify(xml_element, out_stream):
    """
    We use our wwt_data_formats pretty-print, then go back and split attributes
    onto their own lines, alphabetizing them.
    """

    START_ATTR_TAG_RE = re.compile(r"^(\s*)<([-_a-zA-Z0-9]+)\s")
    ATTR_RE = re.compile(r'^\s*(\w+)="([^"]*)"')
    ATTRS_DONE_RE = re.compile(r"^\s*(/?)>$")

    indent_xml(xml_element)

    doc = etree.ElementTree(xml_element)

    with BytesIO() as dest:
        doc.write(dest, encoding="UTF-8", xml_declaration=True)
        bytes = dest.getvalue()

    text = bytes.decode("utf-8")

    for line in text.splitlines():
        m = START_ATTR_TAG_RE.match(line)
        if m is None:
            print(line, file=out_stream)
        else:
            indent = m[1]
            tag = m[2]
            attr_text = line[m.end() :].rstrip()
            attrs = {}
            m_done = ATTRS_DONE_RE.match(attr_text)

            while m_done is None:
                m = ATTR_RE.match(attr_text)
                assert m is not None, f"error chomping attrs in `{line!r}`"

                attr_name = m[1]
                attr_val = m[2]
                assert attr_name not in attrs
                attrs[attr_name] = attr_val

                attr_text = attr_text[m.end() :]
                m_done = ATTRS_DONE_RE.match(attr_text)

            self_ending = bool(m_done[1])

            print(f"{indent}<{tag}", file=out_stream)

            for attr_name, attr_val in sorted(attrs.items()):
                print(f'{indent}  {attr_name}="{attr_val}"', file=out_stream)

            if self_ending:
                print(f"{indent}></{tag}>", file=out_stream)
            else:
                print(f"{indent}>", file=out_stream)


def do_prettify(settings):
    with open(settings.xml, "rt", encoding="utf-8-sig") as f:
        text = f.read()
        elem = etree.fromstring(text)
        prettify(elem, sys.stdout)


# register-cxprep


def do_register_cxprep(settings):
    if settings.dry_run:
        print("NOTE: dry-run mode engaged!\n")

    idb = ImagesetDatabase()
    pdb = PlaceDatabase()
    cxpdb = ConstellationsPrepDatabase()

    try:
        n = cxpdb.register(cx.CxClient(), idb, pdb, dry_run=settings.dry_run)
        print()
        print(f"Registered {n} items")
    finally:
        print("Saving database updates ...")
        idb.rewrite()
        pdb.rewrite()

        if settings.dry_run:
            print("\nWARNING: dry-run mode -- NOT rewriting `cxprep` files!")
            print(
                "  Throw away any imageset/place updates since they will contain fake Constellations IDs!"
            )
        else:
            cxpdb.rewrite()


# replace-urls


def do_replace_urls(settings):
    """
    A specialized utility to update imageset URLs to new URLs, using the
    "AltUrl" mechanism to "save" the old URL, and updating references to them.

    Implemented for Mars panoramas but then I realized I had it backwards! So
    this is untested, but I think it should work.
    """
    idb = ImagesetDatabase()
    replacements = {}

    warn("untested!!!!!")
    warn("tweaking QuadTreeMap and WidthFactor for Mars panos!!!")

    # Imageset database  ...

    with open(settings.spec_path, "rt") as f:
        for line in f:
            old_url, new_url = line.strip().split()
            replacements[old_url] = new_url

            imgset = idb.by_url.pop(old_url, None)
            if imgset is None:
                die(f"missing old-url `{old_url}`")

            imgset.url = new_url
            imgset.alt_url = old_url
            imgset.quad_tree_map = "0123"
            imgset.width_factor = 1
            idb.by_url[new_url] = imgset

    # Place database ...

    pdb = PlaceDatabase()

    def do_one(pinfo, key):
        url = pinfo.get(key)
        if url:
            new_url = replacements.get(url)
            if new_url:
                pinfo[key] = new_url

    for pinfo in pdb.by_uuid.values():
        do_one(pinfo, "image_set_url")
        do_one(pinfo, "background_image_set_url")
        do_one(pinfo, "foreground_image_set_url")

    idb.rewrite()
    pdb.rewrite()

    # Catalog files ...

    def do_one(path: Path):
        with path.open("rt", encoding="utf-8") as f:
            root_info = yaml.load(f, yaml.SafeLoader)

        def replace_folder(info: dict):
            for index in range(len(info["children"])):
                spec = info["children"][index]

                if isinstance(spec, str):
                    if spec.startswith("imageset "):
                        url = spec[9:]
                        new_url = replacements.get(url)
                        if new_url:
                            info["children"][index] = f"imageset {new_url}"
                        f.children.append(idb.get_by_url(spec[9:]))
                else:
                    replace_folder(spec)

        replace_folder(root_info)
        write_one_yaml(path, root_info)

    for path in (BASEDIR / "catfiles").glob("*.yml"):
        do_one(path)


# report


def do_report(_settings):
    idb = ImagesetDatabase()
    print(f"number of imagesets: {len(idb.by_url)}")


# trace


def _trace_catfile(path: Path, pdb: PlaceDatabase, idb: ImagesetDatabase):
    # TODO: fairly redundant with "emit"

    with path.open("rt", encoding="utf-8") as f:
        root_info = yaml.load(f, yaml.SafeLoader)

    def trace_folder(info: dict):
        for spec in info["children"]:
            if isinstance(spec, str):
                if spec.startswith("imageset "):
                    imgset = idb.get_by_url(spec[9:])
                    imgset.rmeta.touched = True
                elif spec.startswith("place "):
                    place = pdb.reconst_by_id(spec[6:], idb)
                    if place.image_set is not None:
                        place.image_set.rmeta.touched = True
                    if place.background_image_set is not None:
                        place.background_image_set.rmeta.touched = True
                    if place.foreground_image_set is not None:
                        place.foreground_image_set.rmeta.touched = True
                else:
                    assert False, f"unexpected terse folder child `{spec}`"
            else:
                # Folder
                url = spec.get("url")

                if not spec["children"] and url:
                    if url.startswith(
                        "http://www.worldwidetelescope.org/wwtweb/catalog.aspx?W="
                    ):
                        catname = url.split("=")[1]
                        catpath = BASEDIR / "catfiles" / f"{catname}.yml"

                        if catpath.exists():
                            print(f"Recursing into `{catname}`", file=sys.stderr)
                            _trace_catfile(catpath, pdb, idb)
                        else:
                            print(
                                f"Skipping `{catname}` which does not seem to be managed by this framework",
                                file=sys.stderr,
                            )
                else:
                    trace_folder(spec)

    trace_folder(root_info)


def do_trace(_settings):
    idb = ImagesetDatabase()
    pdb = PlaceDatabase()

    for imgset in idb.by_url.values():
        imgset.rmeta.touched = False

    warn("this tool needs to be updated to handle `explorerootweb.yml` too")
    _trace_catfile(BASEDIR / "catfiles" / "exploreroot6.yml", pdb, idb)

    for imgset in idb.by_url.values():
        if imgset.rmeta.touched:
            continue

        u = imgset.url
        places = []

        for place in pdb.by_uuid.values():
            if u in (
                place.get("image_set_url"),
                place.get("foreground_image_set_url"),
                place.get("background_image_set_url"),
            ):
                places.append(place["_uuid"])

        if places:
            places = " ".join(places)
        else:
            places = "(no places)"

        print(f"{imgset.url}: {imgset.name} -- {places}")


# update-astropix


def _astropix_associate_chandra(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    """
    AstroPix: IDs are serial numbers as strings, like "102"; URLs are like: `http://chandra.harvard.edu/photo/2003/m86/`
    Us: CreditsUrls are like: `https://chandra.harvard.edu/photo/2003/ngc6888/more.html#img1`
    """
    subset = {}
    assocs = {}

    for imgset in idb.by_url.values():
        cr_url = imgset.credits_url

        if "chandra.harvard.edu" in cr_url:
            subset[cr_url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[cr_url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        apinfo = apimgs[img_id]
        search = apinfo["reference_url"].split("://", 1)[-1]

        for cr_url, imgset in subset.items():
            if search in cr_url:
                assocs[cr_url].add(f"chandra|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for cr_url, apids in assocs.items():
        imgset = subset[cr_url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def _astropix_associate_esahubble(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    subset = {}
    assocs = {}

    for imgset in idb.by_url.values():
        cr_url = imgset.credits_url

        if (
            "spacetelescope.org" in cr_url
            or "esahubble.org" in cr_url
            or "esawebb.org" in cr_url
        ):
            subset[cr_url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[cr_url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        search = f"/{img_id}"

        for cr_url, imgset in subset.items():
            if search in cr_url:
                assocs[cr_url].add(f"esahubble|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for cr_url, apids in assocs.items():
        imgset = subset[cr_url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def _astropix_associate_eso(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    subset = {}
    assocs = {}

    for imgset in idb.by_url.values():
        cr_url = imgset.credits_url

        if "eso.org" in cr_url:
            subset[cr_url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[cr_url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        search = f"/{img_id}"

        for cr_url, imgset in subset.items():
            if search in cr_url:
                assocs[cr_url].add(f"eso|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for cr_url, apids in assocs.items():
        imgset = subset[cr_url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def _astropix_associate_noirlab(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    subset = {}
    assocs = {}

    for imgset in idb.by_url.values():
        cr_url = imgset.credits_url

        if "noirlab.edu" in cr_url:
            subset[cr_url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[cr_url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        search = f"/{img_id}"

        for cr_url, imgset in subset.items():
            if search in cr_url:
                assocs[cr_url].add(f"noirlab|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for cr_url, apids in assocs.items():
        imgset = subset[cr_url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def _astropix_associate_spitzer(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    subset = {}
    assocs = {}

    for imgset in idb.by_url.values():
        cr_url = imgset.credits_url

        if "spitzer.caltech.edu" in cr_url:
            subset[cr_url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[cr_url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        search = f"/{img_id}"

        for cr_url, imgset in subset.items():
            if search in cr_url:
                assocs[cr_url].add(f"spitzer|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for cr_url, apids in assocs.items():
        imgset = subset[cr_url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def _astropix_associate_wise(idb: ImagesetDatabase, apimgs: Dict[str, dict]):
    subset = {}
    assocs = {}

    for url, imgset in idb.by_url.items():
        cr_url = imgset.credits_url

        if "wise.astro.ucla.edu" in cr_url or "wise.ssl.berkeley.edu" in cr_url:
            subset[url] = imgset

            prev = getattr(imgset.xmeta, "astropix_ids", None)
            if prev is None:
                prev_ids = ()
            else:
                prev_ids = prev.split(",")

            assocs[url] = set(prev_ids)

    for img_id in list(apimgs.keys()):
        for url, imgset in subset.items():
            if img_id in imgset.url:
                assocs[url].add(f"wise|{img_id}")
                del apimgs[img_id]  # mark this one as associated
                break

    for url, apids in assocs.items():
        imgset = subset[url]

        if apids:
            imgset.xmeta.astropix_ids = ",".join(sorted(apids))


def do_update_astropix(_settings):
    # Load the AstroPix database

    try:
        with (BASEDIR / "astropix" / "all.json").open("rt") as f:
            ap_all = json.load(f)
    except FileNotFoundError:
        die(
            "you must first download the AstroPix database to `astropix/all.json` (see README.md)"
        )

    # From looking at the current collection, it appears that the best filter to
    # apply is as follows. "Position" quality WCS often apply to artist's
    # conceptions that are associated with a specific object, but should not be
    # placed on the sky.
    ap_all = [item for item in ap_all if item["wcs_quality"] == "Full"]
    print(f"{len(ap_all)} images in the filtered AstroPix database")

    # Load the imagesets and identify the AstroPix IDs that have already been
    # associated.

    idb = ImagesetDatabase()
    done_ids = set()

    for imgset in idb.by_url.values():
        apids = getattr(imgset.xmeta, "astropix_ids", "")
        if apids:
            for apid in apids.split(","):
                done_ids.add(apid)

    if done_ids:
        print(f"{len(done_ids)} images already linked")

    # IDs to perma-ignore

    ignore_ids = set()

    try:
        with (BASEDIR / "astropix" / "ignore.txt").open("rt") as f:
            for line in f:
                apid = line.split("#")[0].strip()
                if apid:
                    if apid in done_ids:
                        warn(
                            f'AstroPix item `{apid}` is in "ignore" list but also "done" list'
                        )
                    ignore_ids.add(apid)
    except FileNotFoundError:
        pass

    if ignore_ids:
        print(f"{len(ignore_ids)} AstroPix IDs in the ignore list")

    # Group by publisher

    by_pubid = {}

    for item in ap_all:
        apid = f"{item['publisher_id']}|{item['image_id']}"
        if apid in done_ids or apid in ignore_ids:
            continue

        by_pubid.setdefault(item["publisher_id"], {})[item["image_id"]] = item

    # Associate

    assocs = {
        "chandra": _astropix_associate_chandra,
        "esahubble": _astropix_associate_esahubble,
        "eso": _astropix_associate_eso,
        "noirlab": _astropix_associate_noirlab,
        "spitzer": _astropix_associate_spitzer,
        "wise": _astropix_associate_wise,
    }

    for pubid, assoc_fn in assocs.items():
        assoc_fn(idb, by_pubid.get(pubid, {}))

    # (Re)write database and lists of unassociated images

    idb.rewrite()

    for pubid, apimgs in sorted(by_pubid.items()):
        prev_imgids = set()
        n_fixed = 0

        try:
            with (BASEDIR / "astropix" / f"{pubid}.csv").open("rt") as f:
                for line in f:
                    imgid = line.split(",")[0]
                    if imgid not in apimgs:
                        n_fixed += 1

                    prev_imgids.add(imgid)
        except FileNotFoundError:
            pass

        n_new = 0
        n_tot = len(apimgs)

        if n_tot:
            with (BASEDIR / "astropix" / f"{pubid}.csv").open("wt") as f:
                w = csv.writer(f)

                for imgid, imgdata in sorted(apimgs.items()):
                    if imgid not in prev_imgids:
                        n_new += 1

                    w.writerow(
                        (
                            imgdata["image_id"],
                            imgdata["reference_url"] or "",
                            (imgdata["title"] or "")
                            .replace("\n", " ")
                            .replace("\r", " "),
                        )
                    )

        if n_tot or n_fixed:
            qpub = f"`{pubid}`"
            print(
                f"publisher {qpub:12}: {n_tot:5} unassociated images; {n_new:4} new; {n_fixed:4} fixed"
            )


# update-cxprep


def do_update_cxprep(_settings):
    idb = ImagesetDatabase()
    pdb = PlaceDatabase()
    cxpdb = ConstellationsPrepDatabase()
    cxpdb.update(idb, pdb)
    cxpdb.rewrite()


# generic driver


def entrypoint():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")

    add_alt_urls = subparsers.add_parser("add-alt-urls")
    add_alt_urls.add_argument(
        "spec_path", metavar="TEXT-PATH", help="Path to text file of URLs to update"
    )

    emit = subparsers.add_parser("emit")
    emit.add_argument(
        "--preview", action="store_true", help="Emit relative-URL files for previewing"
    )

    emit_partition = subparsers.add_parser("emit-partition")
    emit_partition.add_argument(
        "partition_path",
        metavar="PATH",
        help="Path to a text file with the partitioning information",
    )
    emit_partition.add_argument(
        "partition_name",
        metavar="NAME",
        help="The element of the partition to emit",
    )
    emit_partition.add_argument(
        "wtml_path", metavar="WTML-PATH", help="Path to the WTML file to emit"
    )

    emit_searchdata = subparsers.add_parser("emit-searchdata")
    emit_searchdata.add_argument(
        "--pretty-json", action="store_true", help="Emit as indented JSON"
    )
    emit_searchdata.add_argument(
        "catdir",
        metavar="DIR-PATH",
        help="Directory with catalog files",
    )

    forget = subparsers.add_parser("forget")
    forget.add_argument(
        "image_urls",
        metavar="URL",
        nargs="+",
        help="URL(s) of imageset(s) to forget",
    )

    _format_imagesets = subparsers.add_parser("format-imagesets")
    _format_places = subparsers.add_parser("format-places")
    _ground_truth = subparsers.add_parser("ground-truth")

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument(
        "--cx-handle",
        metavar="HANDLE",
        required=True,
        help="The Constellations handle that will eventually own these images",
    )
    ingest.add_argument(
        "--prepend-to",
        metavar="YML-PATH",
        help="Add the new places to the specific, existing catalog file",
    )
    ingest.add_argument(
        "--emit",
        action="store_true",
        help='Emit a new "catfile" YAML for the ingested folder',
    )
    ingest.add_argument(
        "wtml", metavar="WTML-PATH", help="Path to a catalog WTML file to ingest"
    )

    partition = subparsers.add_parser("partition")
    partition.add_argument(
        "path",
        metavar="PATH",
        help="Path to a text file with the partitioning information",
    )

    prettify = subparsers.add_parser("prettify")
    prettify.add_argument(
        "xml", metavar="XML-PATH", help="Path to an XML file to prettify"
    )

    register_cxprep = subparsers.add_parser("register-cxprep")
    register_cxprep.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not actually register",
    )

    replace_urls = subparsers.add_parser("replace-urls")
    replace_urls.add_argument(
        "spec_path", metavar="TEXT-PATH", help="Path to text file of URLs to update"
    )

    _report = subparsers.add_parser("report")
    _trace = subparsers.add_parser("trace")
    _update_astropix = subparsers.add_parser("update-astropix")
    _update_cxprep = subparsers.add_parser("update-cxprep")

    settings = parser.parse_args()

    if settings.subcommand is None:
        die("you must specify a subcommand", prefix="usage error:")
    elif settings.subcommand == "add-alt-urls":
        do_add_alt_urls(settings)
    elif settings.subcommand == "emit":
        do_emit(settings)
    elif settings.subcommand == "emit-partition":
        do_emit_partition(settings)
    elif settings.subcommand == "emit-searchdata":
        do_emit_searchdata(settings)
    elif settings.subcommand == "forget":
        do_forget(settings)
    elif settings.subcommand == "format-imagesets":
        do_format_imagesets(settings)
    elif settings.subcommand == "format-places":
        do_format_places(settings)
    elif settings.subcommand == "ground-truth":
        do_ground_truth(settings)
    elif settings.subcommand == "ingest":
        do_ingest(settings)
    elif settings.subcommand == "partition":
        do_partition(settings)
    elif settings.subcommand == "prettify":
        do_prettify(settings)
    elif settings.subcommand == "register-cxprep":
        do_register_cxprep(settings)
    elif settings.subcommand == "replace-urls":
        do_replace_urls(settings)
    elif settings.subcommand == "report":
        do_report(settings)
    elif settings.subcommand == "trace":
        do_trace(settings)
    elif settings.subcommand == "update-astropix":
        do_update_astropix(settings)
    elif settings.subcommand == "update-cxprep":
        do_update_cxprep(settings)
    else:
        die(f"unknown subcommand `{settings.subcommand}`", prefix="usage error:")


if __name__ == "__main__":
    entrypoint()
