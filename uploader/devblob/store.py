"""Block staging and commit over a directory.

Backs the two routes in :mod:`server` — ``comp=block`` and
``comp=blocklist`` — with the same on-disk layout described in
``uploader/spec/tasks/06-mock-azure.md`` §3::

    {root}/{email}/{client path}          the committed blob
    {root}/{email}/{client path}.meta     JSON sidecar: content_type
    {root}/.staged/{blob path}/{block id} uncommitted blocks

A filesystem has no content type of its own, hence the sidecar: it carries
whatever ``x-ms-blob-content-type`` set at commit, so reconciliation has
something to compare against locally.

Commit order comes from the caller-supplied list of block IDs — the
client's ``<BlockList>`` XML, parsed in :mod:`server` — never from arrival
order or a filename sort. The client stages blocks with six-way
concurrency, so they land on disk out of order routinely; concatenating by
arrival would produce a corrupt file that still looks plausible.

This module knows nothing about HTTP, SAS query strings or Azure's error
taxonomy — that is :mod:`server`'s job. It only knows how to stage a block,
commit a list of them, and answer what has already landed.
"""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STAGED_DIRNAME = ".staged"
META_SUFFIX = ".meta"
TEMP_SUFFIX = ".devblob-tmp"


class BlobAlreadyExistsError(Exception):
    """The target blob already exists. Azure's ``BlobAlreadyExists``.

    ``sp=c`` rather than ``sp=w`` is the security property the whole design
    rests on — a stand-in that silently overwrote would train the wrong
    expectation.
    """


class UnknownBlockError(Exception):
    """The block list names a block that was never staged.

    Azure's ``InvalidBlockList``. Blocks belong to the blob path, not to
    whichever SAS staged them, so this is not a permission problem — it
    means the client's block list and its staged blocks disagree.
    """


@dataclass(frozen=True)
class StoredBlobProperties:
    """The facts about a committed blob, read straight off the filesystem."""

    size: int
    content_type: str
    last_modified: datetime


def _block_filename(block_id: str) -> str:
    """Return a filesystem-safe name for one base64 block ID.

    Azure block IDs are base64 and may contain ``/`` and ``+``, neither of
    which is safe as a bare filename. Hashing sidesteps deciding how much
    of the ID to percent-encode.
    """
    return hashlib.sha256(block_id.encode()).hexdigest()


def _write_atomically(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file and ``os.replace``.

    So a reader never observes a partially written commit target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    tmp.write_bytes(data)
    os.replace(tmp, path)


class DevBlobStore:
    """Stages blocks and commits them into real files under ``root``."""

    def __init__(self, root):
        self.root = Path(root)

    def ensure_ready(self) -> None:
        """Prove ``root`` can be created and written to, or raise.

        Called at process startup, per §7 of the task brief — a stand-in
        that cannot write must refuse to start, not fail on the first
        upload.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".devblob-write-test"
        probe.write_text("ok")
        probe.unlink()

    def _blob_file(self, blob_path: str) -> Path:
        return self.root / blob_path

    def _meta_file(self, blob_path: str) -> Path:
        return self.root / f"{blob_path}{META_SUFFIX}"

    def _staged_dir(self, blob_path: str) -> Path:
        return self.root / STAGED_DIRNAME / blob_path

    def _staged_block_file(self, blob_path: str, block_id: str) -> Path:
        return self._staged_dir(blob_path) / _block_filename(block_id)

    def stage_block(self, blob_path: str, block_id: str, data: bytes) -> None:
        """Write one uncommitted block, keyed by blob path and block ID."""
        block_file = self._staged_block_file(blob_path, block_id)
        block_file.parent.mkdir(parents=True, exist_ok=True)
        block_file.write_bytes(data)

    def commit(
        self,
        blob_path: str,
        block_ids: list,
        content_type: str,
    ) -> None:
        """Concatenate staged blocks, in ``block_ids`` order, into the blob.

        Blocks are looked up by blob path alone — never by which SAS staged
        them — so a renewal (stage under SAS A, commit under SAS B) works
        without special-casing.

        Raises:
            BlobAlreadyExistsError: the target already exists.
            UnknownBlockError: a listed block was never staged.
        """
        target = self._blob_file(blob_path)
        if target.exists():
            raise BlobAlreadyExistsError(blob_path)

        chunks = []
        for block_id in block_ids:
            block_file = self._staged_block_file(blob_path, block_id)
            if not block_file.is_file():
                raise UnknownBlockError(block_id)
            chunks.append(block_file.read_bytes())

        _write_atomically(target, b"".join(chunks))
        _write_atomically(
            self._meta_file(blob_path),
            json.dumps({"content_type": content_type}).encode(),
        )

        shutil.rmtree(self._staged_dir(blob_path), ignore_errors=True)

    def properties(self, blob_path: str) -> StoredBlobProperties:
        """Return a committed blob's size, sidecar type and mtime.

        Returns ``None`` if no committed blob exists at ``blob_path`` —
        staged-but-uncommitted blocks do not count, matching Azure not
        listing a blob until its blocks are committed.
        """
        target = self._blob_file(blob_path)
        if not target.is_file():
            return None

        content_type = None
        meta_file = self._meta_file(blob_path)
        if meta_file.is_file():
            try:
                content_type = json.loads(
                    meta_file.read_text()).get("content_type")
            except (OSError, ValueError):
                content_type = None

        stat_result = target.stat()
        return StoredBlobProperties(
            size=stat_result.st_size,
            content_type=content_type,
            last_modified=datetime.fromtimestamp(
                stat_result.st_mtime, tz=timezone.utc),
        )
