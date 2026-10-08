"""Collector-local evidence survives a database outage, with record deduplication."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from .block_observer import _is_digest, _pack
from .observation import read_archive, write_archive
from .protocol import ProtocolDataError


class Spool:
    def __init__(self, directory, *, paged=False):
        """``paged`` stores record lists as deduplicated digest pages in canonical
        order; use it for block observations only."""
        self.directory = Path(directory)
        self.paged = paged
        (self.directory / "chunks").mkdir(parents=True, exist_ok=True)
        (self.directory / "pending").mkdir(exist_ok=True)
        (self.directory / "recorded").mkdir(exist_ok=True)
        _sync(self.directory)
        _sync(self.directory.parent)

    def save(self, observation, metadata, error=None):
        manifest, chunks, _ = _pack(observation, paged=self.paged)
        for digest, compressed in chunks.items():
            directory = self.directory / "chunks" / digest[:2]
            directory.mkdir(exist_ok=True)
            _sync(self.directory / "chunks")
            destination = directory / (digest + ".gz")
            if destination.exists():
                continue
            descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(compressed)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                _sync(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        path = self.directory / "pending" / (uuid.uuid4().hex + ".json.gz")
        write_archive(path, {"manifest": manifest, "metadata": metadata, "error": error})
        return path

    def read(self, path):
        saved = read_archive(path)

        def load(digest):
            if not _is_digest(digest):
                raise ProtocolDataError("invalid spool digest")
            raw = gzip.decompress((self.directory / "chunks" / digest[:2] / (digest + ".gz")).read_bytes())
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ProtocolDataError("spool evidence checksum mismatch")
            return raw

        def records(digests):
            return [json.loads(load(digest)) for digest in digests]

        def decode(value):
            if "dict" in value:
                return {key: decode(child) for key, child in value["dict"]}
            if "pages" in value:
                result = []
                for page in value["pages"]:
                    raw = load(page)
                    if not raw or len(raw) % 32:
                        raise ProtocolDataError("spool page is not a digest list")
                    result.extend(records(raw[index:index + 32].hex() for index in range(0, len(raw), 32)))
                return result
            if "items" in value:
                result = []
                for page in value["items"]:
                    listing = json.loads(load(page))
                    if not isinstance(listing, list) or not all(isinstance(item, str) for item in listing):
                        raise ProtocolDataError("spool page is not a string list")
                    result.extend(listing)
                return result
            if "blob" in value:
                return json.loads(load(value["blob"]))
            if "records" in value:
                return records(value["records"])
            return value["value"]
        return decode(saved["manifest"]), saved["metadata"], saved["error"]

    def recorded(self, path):
        path = Path(path)
        os.replace(path, self.directory / "recorded" / path.name)
        _sync(self.directory / "recorded")
        _sync(self.directory / "pending")

    def pending(self):
        return sorted((self.directory / "pending").glob("*.json.gz"))


def _sync(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
