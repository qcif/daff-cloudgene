"""Unit tests for :mod:`server`.

Run from ``uploader/devblob``:

    python -m unittest discover -s tests -t .

No test touches the network or Azure. :class:`fastapi.testclient.TestClient`
drives the ASGI app in-process.
"""

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from server import CORS_ALLOWED_HEADERS, create_app
from store import DevBlobStore

CONTAINER = "uploads"
BLOB_PATH = "bob@example.com/run7/reads.fastq.gz"
DEV_ORIGIN = "http://localhost:5173"
SAS_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def sas_query(expiry: datetime = None) -> str:
    """Return a query string shaped like a real (unsigned) SAS."""
    se = (expiry or _future()).strftime(SAS_TIMESTAMP_FORMAT)
    return f"sv=2024-11-04&sr=b&sp=c&se={se}&sig=fake"


def _future(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def block_id(index: int) -> str:
    """Return a base64 block ID, matching the shape the SDK sends."""
    return base64.b64encode(f"block-{index:06d}".encode()).decode()


class ServerTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = create_app(root=self.root)
        self.client = TestClient(self.app)

    def stage(self, blob_path, block_index, body, query=None):
        return self.client.put(
            f"/{CONTAINER}/{blob_path}",
            params={"comp": "block", "blockid": block_id(block_index),
                    **_split_query(query or sas_query())},
            content=body,
            headers={"content-type": "application/octet-stream"},
        )

    def commit(self, blob_path, block_indices, content_type=None,
               query=None):
        xml = "<?xml version='1.0' encoding='utf-8'?><BlockList>" + "".join(
            f"<Latest>{block_id(i)}</Latest>" for i in block_indices
        ) + "</BlockList>"
        headers = {"content-type": "application/xml"}
        if content_type is not None:
            headers["x-ms-blob-content-type"] = content_type
        return self.client.put(
            f"/{CONTAINER}/{blob_path}",
            params={"comp": "blocklist", **_split_query(query or sas_query())},
            content=xml,
            headers=headers,
        )


def _split_query(query: str) -> dict:
    pairs = [part.split("=", 1) for part in query.split("&")]
    return {k: v for k, v in pairs}


class TestBlockAndCommit(ServerTestCase):

    def test_three_blocks_out_of_order_commit_in_block_list_order(self):
        self.stage(BLOB_PATH, 2, b"CCC")
        self.stage(BLOB_PATH, 0, b"AAA")
        self.stage(BLOB_PATH, 1, b"BBB")

        response = self.commit(
            BLOB_PATH, [0, 1, 2], content_type="text/plain")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            (self.root / BLOB_PATH).read_bytes(), b"AAABBBCCC")

    def test_content_type_reaches_properties(self):
        self.stage(BLOB_PATH, 0, b"AAA")
        self.commit(BLOB_PATH, [0], content_type="text/csv")

        properties = DevBlobStore(self.root).properties(BLOB_PATH)
        self.assertEqual(properties.content_type, "text/csv")


class TestCreateOnly(ServerTestCase):

    def test_second_commit_is_409_with_azure_shaped_error(self):
        self.stage(BLOB_PATH, 0, b"AAA")
        first = self.commit(BLOB_PATH, [0])
        self.assertEqual(first.status_code, 201)

        self.stage(BLOB_PATH, 0, b"BBB")
        second = self.commit(BLOB_PATH, [0])

        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.headers["x-ms-error-code"], "BlobAlreadyExists")
        self.assertIn("BlobAlreadyExists", second.text)


class TestRenewalAcrossSas(ServerTestCase):

    def test_blocks_staged_under_one_sas_commit_under_a_later_one(self):
        # A later, later-expiring SAS than the one used to stage — exactly
        # the renewal flow: re-sign, stage under SAS A, commit under SAS B.
        early = sas_query(_future(hours=1))
        later = sas_query(_future(hours=48))

        self.stage(BLOB_PATH, 0, b"AAA", query=early)
        response = self.commit(BLOB_PATH, [0], query=later)

        self.assertEqual(response.status_code, 201)
        self.assertEqual((self.root / BLOB_PATH).read_bytes(), b"AAA")


class TestExpiry(ServerTestCase):

    def test_a_lapsed_se_is_403(self):
        expired = sas_query(_past())
        response = self.stage(BLOB_PATH, 0, b"AAA", query=expired)
        self.assertEqual(response.status_code, 403)
        self.assertIn("x-ms-error-code", response.headers)

    def test_commit_with_a_lapsed_se_is_403(self):
        self.stage(BLOB_PATH, 0, b"AAA")
        expired = sas_query(_past())
        response = self.commit(BLOB_PATH, [0], query=expired)
        self.assertEqual(response.status_code, 403)


class TestUnrecognisedComp(ServerTestCase):

    def test_an_unknown_comp_value_is_400(self):
        query = _split_query(sas_query())
        response = self.client.put(
            f"/{CONTAINER}/{BLOB_PATH}",
            params={"comp": "snapshot", **query},
            content=b"x",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("x-ms-error-code", response.headers)


class TestTraversal(ServerTestCase):

    def test_a_traversal_path_is_400_and_writes_nothing_outside_root(self):
        query = _split_query(sas_query())
        response = self.client.put(
            f"/{CONTAINER}/bob@example.com/../../../etc/evil",
            params={"comp": "block", "blockid": block_id(0), **query},
            content=b"x",
        )
        self.assertEqual(response.status_code, 400)
        written = list(self.root.rglob("*"))
        self.assertEqual(written, [])


class TestCorsPreflight(ServerTestCase):

    def test_preflight_allows_the_measured_request_headers(self):
        # x-ms-useragent is the header a live browser run turned up that
        # static measurement of the SDK's request-building code missed —
        # see the escalation in
        # uploader/spec/tasks/completed/2_azure_resources.md.
        response = self.client.options(
            f"/{CONTAINER}/{BLOB_PATH}",
            headers={
                "Origin": DEV_ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "x-ms-version, x-ms-client-request-id, "
                    "x-ms-useragent, content-type"),
            },
        )
        self.assertEqual(response.status_code, 200)
        allowed = response.headers.get(
            "access-control-allow-headers", "").lower()
        for header in (
                "x-ms-version", "x-ms-client-request-id",
                "x-ms-useragent"):
            self.assertIn(header, allowed)

    def test_only_the_dev_origin_is_allowed(self):
        response = self.client.options(
            f"/{CONTAINER}/{BLOB_PATH}",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertNotIn(
            "access-control-allow-origin", response.headers)

    def test_expected_headers_are_configured(self):
        # Guards the constant the middleware is built from, so a future
        # edit that trims it breaks a test loudly.
        self.assertIn("x-ms-version", CORS_ALLOWED_HEADERS)
        self.assertIn("x-ms-client-request-id", CORS_ALLOWED_HEADERS)
        self.assertIn("x-ms-useragent", CORS_ALLOWED_HEADERS)


if __name__ == "__main__":
    unittest.main()
