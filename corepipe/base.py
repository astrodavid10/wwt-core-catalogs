# Copyright the WorldWide Telescope project
# Licensed under the MIT License.

"""
Base classes for the pipeline framework
"""

__all__ = """
CandidateInput
ImageSource
IMAGE_SOURCE_CLASS_LOADERS
NotActionableError
PipelineIo
PIPELINE_IO_LOADERS
""".split()

from abc import ABC, abstractmethod
from collections import OrderedDict
from io import BytesIO
import json
import os.path
import shutil
import sys
from typing import BinaryIO, Iterable, Tuple
import uuid
import yaml

import toasty
from wwt_api_client import constellations as cx
from wwt_data_formats.folder import Folder, make_absolutizing_url_mutator

from cattool import (
    BASEDIR,
    ImagesetDatabase,
    PlaceDatabase,
    _emit_record,
    _parse_record_file,
    _register_image,
    _register_scene,
    warn,
    write_one_yaml,
)


class NotActionableError(Exception):
    """
    Raised when an image is provided to the pipeline but for some reason we're
    not going to be able to get it into a WWT-compatible form.
    """

    def __init__(self, reason):
        super(NotActionableError, self).__init__(reason)


# This will be populated by other imports
PIPELINE_IO_LOADERS = {}


class PipelineIo(ABC):
    """
    An abstract base class for I/O relating to pipeline processing. An instance
    of this class might be used to fetch files from, and send them to, a cloud
    storage system like S3 or Azure Storage.
    """

    @abstractmethod
    def _export_config(self) -> dict:
        """
        Export this object's configuration for serialization.

        Returns
        -------
        A dictionary of settings that can be saved as YAML format. There should
        be a key named "_type" with a string value identifying the I/O
        implementation type.
        """

    def save_config(self, path: str):
        """
        Save this object's configuration to the specified filesystem path.
        """
        cfg = self._export_config()

        # The config contains secrets, so create it privately and securely.
        opener = lambda path, _mode: os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600
        )

        with open(path, "wt", opener=opener, encoding="utf8") as f:
            yaml.dump(cfg, f, yaml.SafeDumper)

    @classmethod
    @abstractmethod
    def _new_from_config(cls, config: dict) -> "PipelineIo":
        """
        Create a new instance of this class based on serialized configuration.

        Parameters
        ----------
        config : dict
            A dict of configuration that was created with ``_export_config``

        Returns
        -------
        A new instance of the class.
        """

    @classmethod
    def load_from_config(self, path: str) -> "PipelineIo":
        """
        Create a new I/O backend from saved configuration.

        Parameters
        ----------
        path : path-like
            The path where the configuration was saved.

        Returns
        -------
        A new instance implementing the PipelineIO abstract base class.
        """

        with open(path, "rt", encoding="utf8") as f:
            config = yaml.safe_load(f)

        ty = config.get("_type")
        loader = PIPELINE_IO_LOADERS.get(ty)
        if loader is None:
            raise Exception(f"unrecognized pipeline I/O storage type {ty!r}")

        return loader(config)

    @abstractmethod
    def check_exists(self, *path: Iterable[str]) -> bool:
        """
        Test whether an item at the specified path exists.

        Parameters
        ----------
        *path : strings
            The path to the item, intepreted as components in a folder
            hierarchy.

        Returns
        -------
        A boolean indicating whether the item in question exists.
        """

    @abstractmethod
    def get_item(self, *path: Iterable[str], dest: BinaryIO = None):
        """
        Fetch a file-like item at the specified path, writing its contents into
        the specified file-like object *dest*.

        Parameters
        ----------
        *path : strings
            The path to the item, intepreted as components in a folder
            hierarchy.
        dest : writeable file-like object
            The object into which the item's data will be written as bytes.

        Returns
        -------
        None.
        """

    @abstractmethod
    def put_item(self, *path: Iterable[str], source: BinaryIO = None):
        """
        Put a file-like item at the specified path, reading its contents from
        the specified file-like object *source*.

        Parameters
        ----------
        *path : strings
            The path to the item, intepreted as components in a folder
            hierarchy.
        source : readable file-like object
            The object from which the item's data will be read, as bytes.

        Returns
        -------
        None.
        """

    @abstractmethod
    def list_items(self, *path: Iterable[str]) -> Iterable[Tuple[str, bool]]:
        """
        List the items contained in the folder at the specified path.

        Parameters
        ----------
        *path : strings
            The path to the item, intepreted as components in a folder
            hierarchy.

        Returns
        -------
        An iterable of ``(stem, is_folder)``, where *stem* is the "basename" of
        an item contained within the specified folder and *is_folder* is a
        boolean indicating whether this item appears to be a folder itself.
        """


class ImageSource(ABC):
    """
    An abstract base class representing a source of images to be processed in
    the image-processing pipeline. An instance of this class might fetch images
    from an RSS feed or an AstroPix search.
    """

    @classmethod
    @abstractmethod
    def get_config_key(cls) -> str:
        """
        Get the name of the section key used for this source's configuration
        data.

        Returns
        -------
        A string giving a key name usable in a YAML file.
        """

    @classmethod
    @abstractmethod
    def deserialize(cls, data: dict) -> "ImageSource":
        """
        Create an instance of this class by deserializing configuration data.

        Parameters
        ----------
        data : dict-like object
            A dict-like object containing configuration items deserialized from
            a format such as JSON or YAML. The particular contents can vary
            depending on the implementation.

        Returns
        -------
        An instance of *cls*.
        """

    @abstractmethod
    def query_candidates(self) -> Iterable["CandidateInput"]:
        """
        Generate a sequence of candidate input images that the pipeline may want
        to process.

        Returns
        -------
        A generator that yields a sequence of `CandidateInput` instances.
        """

    @abstractmethod
    def fetch_candidate(
        self, unique_id: str, cand_data_stream: BinaryIO, cachedir: str
    ):
        """
        Download a candidate image and prepare it for processing.

        Parameters
        ----------
        unique_id : str
            The unique ID returned by the `CandidateInput` instance that was
            returned from the initial query.
        cand_data_stream : readable stream returning bytes
            A data stream returning the data that were saved when the candidate
            was queried (`CandidateInput.save()`).
        cachedir : path-like
            A path pointing to a local directory inside of which the full image
            data and metadata should be cached.
        """

    @abstractmethod
    def process(
        self,
        unique_id: str,
        cand_data_stream: BinaryIO,
        cachedir: str,
        builder: "toasty.builder.Builder",
    ):
        """
        Process an input into WWT format.

        Parameters
        ----------
        unique_id : str
            The unique ID returned by the :class:`toasty.pipeline.CandidateInput` instance
            that was returned from the initial query.
        cand_data_stream : readable stream returning bytes
            A data stream returning the data that were saved when the candidate
            was queried (:meth:`toasty.pipeline.CandidateInput.save`).
        cachedir : path-like
            A path pointing to a local directory inside of which the
            full image data and metadata should be cached.
        builder : `toasty.builder.Builder`
            State object for constructing the WWT data files.

        Notes
        -----
        Your image processor should run the tile cascade, if needed, but the
        caller will take care of emitting the ``index_rel.wtml`` file.
        """


class CandidateInput(ABC):
    """
    An abstract base class representing an image from one of our sources. If it
    has not been processed before, we will fetch its data and queue it for
    processing.
    """

    @abstractmethod
    def get_unique_id(self) -> str:
        """
        Get an ID for this image that will be unique in its `ImageSource`.

        Returns
        -------
        An identifier as a string. Should be limited to path-friendly
        characters, i.e. ASCII without spaces.
        """

    @abstractmethod
    def save(self, stream: BinaryIO):
        """
        Serialize candidate information for future processing

        Parameters
        ----------
        stream : writeable stream accepting bytes
           The stream into which the candidate information should be serialized.

        Raises
        ------
        May raise :exc:`toasty.pipeline.NotActionableError` if it turns out that this
        candidate is not one that can be imported into WWT.

        Returns
        -------
        None.
        """


# The PipelineManager class that orchestrates it all

# This will be populated by other imports
IMAGE_SOURCE_CLASS_LOADERS = {}


class PipelineManager(object):
    _config: dict = None
    _pipeio: PipelineIo = None
    _workdir: str = None
    _img_source: ImageSource = None

    def __init__(self, workdir: str):
        self._workdir = workdir
        self._pipeio = PipelineIo.load_from_config(self._path("corepipe-storage.yaml"))

    def _path(self, *path: Iterable[str]) -> str:
        return os.path.join(self._workdir, *path)

    def _ensure_dir(self, *path: Iterable[str]) -> str:
        path = self._path(*path)
        os.makedirs(path, exist_ok=True)
        return path

    def ensure_config(self) -> dict:
        if self._config is not None:
            return self._config

        self._ensure_dir()
        cfg_path = self._path("corepipe-config.yaml")

        if not os.path.exists(cfg_path):  # racey
            with open(cfg_path, "wb") as f:
                self._pipeio.get_item("corepipe-config.yaml", dest=f)

        with open(cfg_path, "rt", encoding="utf8") as f:
            config = yaml.safe_load(f)

        if config is None:
            raise Exception("no `corepipe-config.yaml` found in the storage")

        self._config = config
        return self._config

    def feed_id(self) -> str:
        return self.ensure_config()["feed_id"]

    def get_image_source(self) -> ImageSource:
        if self._img_source is not None:
            return self._img_source

        self.ensure_config()

        source_type = self._config.get("source_type")
        if not source_type:
            raise Exception("toasty pipeline configuration must have a source_type key")

        cls_loader = IMAGE_SOURCE_CLASS_LOADERS.get(source_type)
        if cls_loader is None:
            raise Exception(f"unrecognized image source type `{source_type}`")

        cls = cls_loader()
        cfg_key = cls.get_config_key()
        source_config = self._config.get(cfg_key)
        if source_config is None:
            raise Exception(
                f"no image source configuration key `{cfg_key}` in the config file"
            )

        self._img_source = cls.deserialize(source_config)
        return self._img_source

    def process_todos(self):
        from toasty.builder import Builder
        from toasty import par_util
        from toasty.pyramid import PyramidIO

        # Load "prep" file for rewriting

        prep_path = self._path("prep.txt")
        seen_ids = set()

        try:
            with open(prep_path, "rt", encoding="utf-8") as f:
                prep_items = list(_parse_record_file(f, prep_path))
        except FileNotFoundError:
            prep_items = []

        for kind, fields in prep_items:
            seen_ids.add(fields["corepipe_id"])

        # Load AstroPix database for cross-matching

        astropix_pubid = self.ensure_config().get("astropix_publisher_id")
        astropix_imgids = set()

        if astropix_pubid:
            try:
                with (BASEDIR / "astropix" / "all.json").open(
                    "rt", encoding="utf-8"
                ) as f:
                    ap_all = json.load(f)
            except FileNotFoundError:
                warn(
                    "unable to make AstroPix associations; download the AstroPix database to `astropix/all.json` (see README.md)"
                )

            for item in ap_all:
                if item["publisher_id"] != astropix_pubid:
                    continue

                if item["wcs_quality"] != "Full":
                    continue

                astropix_imgids.add(item["image_id"])

        # Let's get going

        src = self.get_image_source()
        cand_dir = self._path("candidates")
        self._ensure_dir("cache_done")
        self._ensure_dir("cache_failed")

        # Lame hack to tidy up output slightly
        par_util.SHOW_INFORMATIONAL_MESSAGES = False

        def _write_prep():
            with open(self._path("prep.txt"), "wt", encoding="utf-8") as f:
                for kind, fields in prep_items:
                    _emit_record(kind, fields, f)

        for uniq_id in os.listdir(self._path("cache_todo")):
            cachedir = self._path("cache_todo", uniq_id)
            outdir = self._path("processed", uniq_id)

            pio = PyramidIO(outdir, scheme="LXY", default_format="png")
            builder = Builder(pio)

            print(f"processing {uniq_id} ... ", end="")
            sys.stdout.flush()

            # Don't let one bad image (e.g. an unreadable source file) abort the
            # whole batch; set it aside in `cache_failed` and keep going.
            try:
                cdata = open(os.path.join(cand_dir, uniq_id), "rb")
                try:
                    src.process(uniq_id, cdata, cachedir, builder)
                finally:
                    cdata.close()
                builder.write_index_rel_wtml()
            except Exception as e:
                print(f"FAILED: {e}")
                shutil.rmtree(outdir, ignore_errors=True)
                os.rename(cachedir, self._path("cache_failed", uniq_id))
                continue

            print("done")

            # Woohoo, done!
            os.rename(cachedir, self._path("cache_done", uniq_id))

            if uniq_id not in seen_ids:
                # Generate records for the prep file

                fields = OrderedDict()
                fields["corepipe_id"] = uniq_id
                fields["cx_handle"] = self._config["default_constellations_handle"]
                fields["prepend_catfile"] = self._config["default_prepend_catfile"]
                fields["copyright"] = self._config["default_copyright"]
                fields["license_id"] = self._config["default_license_id"]

                # NOTE: Hardcoding invariant that AstroPix IDs and our IDs are the
                # same.
                if uniq_id in astropix_imgids:
                    fields["astropix_id"] = f"{astropix_pubid}|{uniq_id}"

                fields["outgoing_url"] = builder.imgset.credits_url
                fields["text"] = builder.place.description
                fields["credits"] = builder.imgset.credits
                fields["wip"] = "yes"
                prep_items.append(("corepipe_image", fields))

            # Persist after each image so a later failure can't lose progress.
            _write_prep()

        _write_prep()

    def upload(self):
        prep_path = self._path("prep.txt")
        self._ensure_dir("uploaded")

        pub_url_prefix = self.ensure_config().get("publish_url_prefix")
        if pub_url_prefix:
            if pub_url_prefix[-1] != "/":
                pub_url_prefix += "/"

        # Load up all of the databases that we're going to edit

        idb = ImagesetDatabase()
        pdb = PlaceDatabase()

        with open(prep_path, "rt", encoding="utf-8") as f:
            items = list(_parse_record_file(f, prep_path))

        catfile_prepends = {}
        catfile_existing = {}

        for kind, fields in items:
            if "wip" in fields:
                continue

            catfile = fields.get("prepend_catfile")
            if catfile:
                catpath = BASEDIR / "catfiles" / f"{catfile}.yml"

                with open(catpath, "rt", encoding="utf-8") as f:
                    catfile_existing[catfile] = yaml.load(f, yaml.SafeLoader)

        # Now we can actually do the main processing

        handle_clients = {}
        cx_client = cx.CxClient()
        remove_ids = set()

        try:
            for kind, fields in items:
                if "wip" in fields:
                    continue

                assert kind == "corepipe_image"
                uniq_id = fields["corepipe_id"]
                already_uploaded = os.path.exists(self._path("uploaded", uniq_id))

                if already_uploaded:
                    print(
                        f"{uniq_id}: image data already uploaded; resuming registration"
                    )
                    wtml_dir = "uploaded"
                else:
                    wtml_dir = "processed"

                index_rel_path = self._path(wtml_dir, uniq_id, "index_rel.wtml")
                index_full_path = self._path(wtml_dir, uniq_id, "index.wtml")

                if already_uploaded:
                    try:
                        f = Folder.from_file(index_full_path)
                    except FileNotFoundError as e:
                        raise Exception(
                            f"found directory `{self._path(wtml_dir, uniq_id)}` but it was missing the `index.wtml`"
                        ) from e
                    place = f.children[0]
                    imgset = place.foreground_image_set
                else:
                    # Construct the final WTML information

                    try:
                        f = Folder.from_file(index_rel_path)
                    except FileNotFoundError as e:
                        raise Exception(
                            f"missing WTML `{self._path('processed', uniq_id, 'index_rel.wtml')}` "
                            f"or `{self._path('uploaded', uniq_id, 'index.wtml')}` for ID `{uniq_id}`"
                        ) from e

                    place = f.children[0]
                    imgset = place.foreground_image_set

                    place.description = fields["text"]
                    imgset.credits = fields["credits"]
                    imgset.credits_url = fields["outgoing_url"]
                    imgset.description = fields["text"]

                    with open(index_rel_path, "wt", encoding="utf8") as f_out:
                        f.write_xml(f_out)

                    # Construct the non-relative index

                    prefix = pub_url_prefix + uniq_id + "/"
                    f.mutate_urls(make_absolutizing_url_mutator(prefix))

                    with open(index_full_path, "wt", encoding="utf8") as f_out:
                        f.write_xml(f_out)

                    # Upload the data
                    #
                    # Save the index.wtml for last -- it will indicate that the tree
                    # has uploaded fully successfully.

                    print(f"{uniq_id}: uploading ...")

                    filenames = os.listdir(self._path("processed", uniq_id))

                    try:
                        index_index = filenames.index("index.wtml")
                    except ValueError:
                        pass
                    else:
                        temp = filenames[-1]
                        filenames[-1] = "index.wtml"
                        filenames[index_index] = temp

                    for filename in filenames:
                        with open(
                            self._path("processed", uniq_id, filename), "rb"
                        ) as f:
                            self._pipeio.put_item(uniq_id, filename, source=f)

                    os.rename(
                        self._path("processed", uniq_id),
                        self._path("uploaded", uniq_id),
                    )

                # Now that it's uploaded, we can register the image with Constellations

                cx_handle = fields["cx_handle"]
                handle_client = handle_clients.get(cx_handle)

                if handle_client is None:
                    handle_client = cx_client.handle_client(cx_handle)
                    handle_clients[cx_handle] = handle_client

                cx_img_id = _register_image(handle_client, fields, imgset)

                # ... and the place/scene

                apid = fields.get("astropix_id")
                place_uuid = str(uuid.uuid4())
                fields["place_uuid"] = place_uuid
                cx_scene_id = _register_scene(
                    handle_client,
                    fields,
                    place,
                    cx_img_id,
                    apid=apid,
                    published=False,
                )

                # Next, add to the local databases

                imgset.xmeta.cxstatus = f"in:{cx_img_id}"
                imgset.xmeta.corepipe_ids = f"{self.feed_id()}|{uniq_id}"

                if apid:
                    imgset.xmeta.astropix_ids = apid

                pdb.ingest_place(place, idb, new_id=place_uuid)
                pdb.by_uuid[place_uuid]["cxstatus"] = f"in:{cx_scene_id}"

                catfile = fields.get("prepend_catfile")
                if catfile:
                    catfile_prepends.setdefault(catfile, []).append(
                        f"place {place_uuid}"
                    )

                remove_ids.add(uniq_id)
        finally:
            print("Saving database updates ...")

            idb.rewrite()
            pdb.rewrite()

            for catfile, prepends in catfile_prepends.items():
                existing = catfile_existing[catfile]
                existing["children"] = prepends[::-1] + existing["children"]
                write_one_yaml(BASEDIR / "catfiles" / f"{catfile}.yml", existing)

            # Update the list of items. We take this particular approach so
            # that if we crash mid-operation, progress will be correctly
            # saved, to the best of our ability.

            new_items = []

            for kind, fields in items:
                if fields["corepipe_id"] in remove_ids:
                    continue

                new_items.append((kind, fields))

            with open(prep_path, "wt", encoding="utf-8") as f:
                for kind, fields in new_items:
                    _emit_record(kind, fields, f)

    def ignore_rejects(self):
        rejects_dir = self._path("rejects")
        n = 0

        # maybe one day this will be JSON with data?
        flag_content = BytesIO(b"{}")

        for uniq_id in os.listdir(rejects_dir):
            print(f"ignoring {uniq_id} ...")
            self._pipeio.put_item(uniq_id, "skip.flag", source=flag_content)
            n += 1

        if n > 1:
            print()
            print(f"marked a total of {n} images to be permanently ignored")
