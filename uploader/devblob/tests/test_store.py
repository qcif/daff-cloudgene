"""Unit tests for :mod:`store`.

Run from ``uploader/devblob``:

    python -m unittest discover -s tests -t .

No test touches the network.
"""

import json
import tempfile
import unittest
from pathlib import Path

from store import (
    BlobAlreadyExistsError,
    DevBlobStore,
    STAGED_DIRNAME,
    UnknownBlockError,
)

BLOB_PATH = "bob@example.com/run7/reads.fastq.gz"


class StoreTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DevBlobStore(Path(self.tmp.name))


class TestEnsureReady(StoreTestCase):

    def test_creates_a_missing_root(self):
        nested = Path(self.tmp.name) / "a" / "b"
        store = DevBlobStore(nested)
        store.ensure_ready()
        self.assertTrue(nested.is_dir())


class TestCommitOrdering(StoreTestCase):
    """§3 of the task brief: order comes from the block list, never from
    arrival order or a filename sort — the client stages with six-way
    concurrency, so blocks land out of order routinely."""

    def test_blocks_staged_out_of_order_commit_in_block_list_order(self):
        # Stage in an order that does not match either the block list or a
        # filename sort of the IDs.
        self.store.stage_block(BLOB_PATH, "block-002", b"CCC")
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.store.stage_block(BLOB_PATH, "block-001", b"BBB")

        self.store.commit(
            BLOB_PATH,
            ["block-000", "block-001", "block-002"],
            content_type="text/plain",
        )

        target = Path(self.tmp.name) / BLOB_PATH
        self.assertEqual(target.read_bytes(), b"AAABBBCCC")

    def test_staged_blocks_are_removed_after_commit(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.store.commit(BLOB_PATH, ["block-000"], content_type=None)
        staged_dir = Path(self.tmp.name) / STAGED_DIRNAME / BLOB_PATH
        self.assertFalse(staged_dir.exists())


class TestCreateOnly(StoreTestCase):

    def test_a_second_commit_to_the_same_path_is_refused(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.store.commit(BLOB_PATH, ["block-000"], content_type=None)

        self.store.stage_block(BLOB_PATH, "block-000", b"BBB")
        with self.assertRaises(BlobAlreadyExistsError):
            self.store.commit(BLOB_PATH, ["block-000"], content_type=None)


class TestRenewalAcrossSas(StoreTestCase):
    """Blocks belong to the blob path, not to whichever SAS staged them —
    the renewal flow depends on this."""

    def test_blocks_staged_under_one_sas_commit_under_a_second(self):
        # Nothing here is SAS-specific: the point is that staging and
        # committing need not agree on any token, only on the blob path.
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.store.stage_block(BLOB_PATH, "block-001", b"BBB")

        self.store.commit(
            BLOB_PATH, ["block-000", "block-001"], content_type="text/plain")

        target = Path(self.tmp.name) / BLOB_PATH
        self.assertEqual(target.read_bytes(), b"AAABBB")


class TestUnknownBlock(StoreTestCase):

    def test_committing_an_unstaged_block_id_is_refused(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        with self.assertRaises(UnknownBlockError):
            self.store.commit(
                BLOB_PATH, ["block-000", "block-999"], content_type=None)

    def test_nothing_is_written_when_a_block_is_missing(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        with self.assertRaises(UnknownBlockError):
            self.store.commit(
                BLOB_PATH, ["block-000", "block-999"], content_type=None)
        target = Path(self.tmp.name) / BLOB_PATH
        self.assertFalse(target.exists())


class TestProperties(StoreTestCase):

    def test_uncommitted_blocks_have_no_properties(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.assertIsNone(self.store.properties(BLOB_PATH))

    def test_content_type_reaches_the_sidecar(self):
        self.store.stage_block(BLOB_PATH, "block-000", b"AAA")
        self.store.commit(
            BLOB_PATH, ["block-000"], content_type="text/csv")

        properties = self.store.properties(BLOB_PATH)
        self.assertEqual(properties.content_type, "text/csv")
        self.assertEqual(properties.size, 3)

        meta_file = Path(self.tmp.name) / f"{BLOB_PATH}.meta"
        self.assertEqual(
            json.loads(meta_file.read_text())["content_type"], "text/csv")


if __name__ == "__main__":
    unittest.main()
