"""Route-level tests for app, via FastAPI's TestClient.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

No network and no Azure credentials. Two substitutions make that possible:

- the SAS issuer is :class:`azure_sas.FakeSasIssuer`, selected by
  ``UPLOADER_SAS_ISSUER=fake``;
- the Cloudgene round trip is replaced, but the **decision** is not. Rather
  than stubbing ``validate_token`` with a canned return value, each test
  picks one of the real recorded response bodies in ``fixtures/`` and runs
  it through the real :func:`cloudgene_auth.interpret_server_info`. The
  §4.7 error taxonomy is therefore driven by what production actually
  returned on 2026-09-14, not by what this test file imagines it returns.
"""

import dataclasses
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import app as app_module
import config as config_module
import storage
from cloudgene_auth import (
    CloudgeneContractError,
    CloudgeneUnavailableError,
    interpret_server_info,
)
from config import ConfigError
from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ACCOUNT = "daffstandard"
CONTAINER = "uploads"
OTHER_EMAIL = "someone.else@example.com"

VALID_BODY = {
    "filename": "run7/reads_R1.fastq.gz",
    "size": 1024,
    "content_type": "application/gzip",
}


def load_fixture(name: str) -> dict:
    """Return a recorded ``/api/v2/server`` response body."""
    with (FIXTURES_DIR / name).open() as f:
        return json.load(f)


ANONYMOUS = load_fixture("anonymous.json")
AUTHORISED = load_fixture("authorised.json")
UNENTITLED = load_fixture("unentitled.json")
EMAIL = AUTHORISED["user"]["mail"].lower()


class AppTestCase(unittest.TestCase):
    """An app on a throwaway database, with a fake issuer and no network."""

    rate_limit_max = 100

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        env = {
            "UPLOADER_SAS_ISSUER": "fake",
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
            "UPLOADER_DB_PATH": str(
                Path(self.tmp.name) / "uploads.sqlite3"),
            "UPLOADER_RATE_LIMIT_MAX": str(self.rate_limit_max),
            "UPLOADER_RATE_LIMIT_WINDOW_SECONDS": "3600",
            "UPLOADER_MAX_UPLOAD_BYTES": str(10 * 1024 * 1024),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        # A test must never be affected by whatever .env a developer
        # happens to have on disk — point config.py at a path guaranteed
        # not to exist instead of the real uploader/.env.
        dotenv_patcher = mock.patch.object(
            config_module, "DOTENV_PATH",
            Path(self.tmp.name) / "unused.env")
        dotenv_patcher.start()
        self.addCleanup(dotenv_patcher.stop)

        # The startup self-test is the one thing that must reach Cloudgene;
        # it is stubbed here and covered by test_cloudgene_auth.
        self_test = mock.patch.object(app_module, "startup_self_test")
        self.self_test = self_test.start()
        self.addCleanup(self_test.stop)

        validate = mock.patch.object(app_module, "validate_token")
        self.validate = validate.start()
        self.addCleanup(validate.stop)
        self.use_cloudgene_response(AUTHORISED)

        self.app = app_module.create_app()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # -- helpers ---------------------------------------------------------

    def use_cloudgene_response(self, body: dict) -> None:
        """Route auth through the real decision logic on a real body."""
        self.validate.side_effect = lambda token: interpret_server_info(body)

    def use_cloudgene_error(self, exc: Exception) -> None:
        """Make the Cloudgene round trip fail."""
        self.validate.side_effect = exc

    @property
    def store(self):
        """Return the app's upload store."""
        return self.app.state.store

    @property
    def issuer(self):
        """Return the app's fake SAS issuer."""
        return self.app.state.issuer

    def post_upload(self, **overrides):
        """POST /uploads with a valid body plus any overrides."""
        return self.client.post(
            "/uploads",
            json={**VALID_BODY, **overrides},
            headers={"X-Auth-Token": "a.token.value"},
        )

    def issue(self, **overrides) -> dict:
        """Issue one upload and return the response body."""
        response = self.post_upload(**overrides)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class TestHealthz(AppTestCase):
    """Liveness for the process, not for the dependency chain."""

    def test_healthz_is_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_healthz_needs_no_token(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.validate.assert_not_called()

    def test_healthz_does_not_call_cloudgene_even_when_it_is_down(self):
        self.use_cloudgene_error(CloudgeneUnavailableError("down"))
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.validate.assert_not_called()


class TestIssuance(AppTestCase):

    def test_authorised_user_gets_a_sas(self):
        body = self.issue()
        self.assertIn("upload_url", body)
        self.assertIn("sp=c", body["upload_url"])
        self.assertIn("sr=b", body["upload_url"])
        self.assertIn("spr=https", body["upload_url"])

    def test_blob_path_is_prefixed_with_the_resolved_email(self):
        body = self.issue()
        self.assertEqual(
            body["blob_path"], f"{EMAIL}/run7/reads_R1.fastq.gz")

    def test_upload_id_is_opaque_and_unique(self):
        first = self.issue()["upload_id"]
        second = self.issue()["upload_id"]
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_a_pending_record_is_persisted(self):
        body = self.issue()
        record = self.store.get(body["upload_id"], EMAIL)
        self.assertEqual(record.state, storage.STATE_PENDING)
        self.assertEqual(record.declared_size, VALID_BODY["size"])
        self.assertEqual(
            record.declared_content_type, VALID_BODY["content_type"])

    def test_expiry_is_twenty_four_hours_by_default(self):
        body = self.issue()
        record = self.store.get(body["upload_id"], EMAIL)
        granted = record.expires_at - record.issued_at
        self.assertAlmostEqual(
            granted.total_seconds(), 86400, delta=5)

    def test_client_supplied_identity_is_ignored(self):
        # §12 of the spec: a client that names a different user gets no
        # effect from it.
        body = self.issue(
            email=OTHER_EMAIL,
            username="someone-else",
            user=OTHER_EMAIL,
            role="admin",
            blob_path=f"{OTHER_EMAIL}/pwned.txt",
        )
        self.assertTrue(body["blob_path"].startswith(f"{EMAIL}/"))
        self.assertNotIn(OTHER_EMAIL, body["blob_path"])

    def test_status_read_never_returns_the_sas(self):
        body = self.issue()
        response = self.client.get(
            f"/uploads/{body['upload_id']}",
            headers={"X-Auth-Token": "a.token.value"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("upload_url", response.json())
        self.assertNotIn("sig=", response.text)


class TestAuthTaxonomy(AppTestCase):
    """Every row of §4.7, driven from the recorded fixtures."""

    def test_anonymous_body_is_401(self):
        self.use_cloudgene_response(ANONYMOUS)
        response = self.post_upload()
        self.assertEqual(response.status_code, 401)

    def test_401_body_is_generic(self):
        self.use_cloudgene_response(ANONYMOUS)
        response = self.post_upload()
        self.assertNotIn("token", response.text.lower())
        self.assertIn("detail", response.json())

    def test_missing_header_is_401(self):
        self.use_cloudgene_response(ANONYMOUS)
        response = self.client.post("/uploads", json=VALID_BODY)
        self.assertEqual(response.status_code, 401)

    def test_unentitled_user_is_403(self):
        self.use_cloudgene_response(UNENTITLED)
        response = self.post_upload()
        self.assertEqual(response.status_code, 403)
        self.assertIn("workflow", response.json()["detail"])

    def test_unentitled_user_gets_no_sas(self):
        self.use_cloudgene_response(UNENTITLED)
        response = self.post_upload()
        self.assertNotIn("upload_url", response.text)
        self.assertEqual(self.issuer.issued, [])

    def test_deprecated_and_experimental_apps_do_not_authorise(self):
        # Settled 2026-09-14: only 'apps' counts. A user entitled solely to
        # a deprecated or experimental app is denied, deliberately.
        body = dict(
            UNENTITLED,
            deprecatedApps=[{"id": "taxodactyl_old"}],
            experimentalApps=[{"id": "taxodactyl_dev"}],
        )
        self.use_cloudgene_response(body)
        self.assertEqual(self.post_upload().status_code, 403)

    def test_account_with_no_email_is_403_not_503(self):
        self.use_cloudgene_response(dict(AUTHORISED, user={"mail": None}))
        response = self.post_upload()
        self.assertEqual(response.status_code, 403)

    def test_no_email_message_tells_the_user_what_to_fix(self):
        self.use_cloudgene_response(dict(AUTHORISED, user={"mail": None}))
        self.assertIn(
            "no email address", self.post_upload().json()["detail"])

    def test_cloudgene_unreachable_is_503(self):
        self.use_cloudgene_error(CloudgeneUnavailableError("refused"))
        response = self.post_upload()
        self.assertEqual(response.status_code, 503)

    def test_contract_violation_is_503_and_logged_loudly(self):
        self.use_cloudgene_response(dict(AUTHORISED, loggedIn="true"))
        with self.assertLogs("uploader", level="ERROR") as logs:
            response = self.post_upload()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(
            any("CONTRACT VIOLATION" in line for line in logs.output))

    def test_contract_error_body_is_generic(self):
        self.use_cloudgene_error(CloudgeneContractError("apps is now a dict"))
        response = self.post_upload()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("apps is now a dict", response.text)

    def test_auth_decisions_are_logged_without_the_token(self):
        with self.assertLogs("uploader", level="INFO") as logs:
            self.post_upload()
        joined = "\n".join(logs.output)
        self.assertIn(EMAIL, joined)
        self.assertNotIn("a.token.value", joined)

    def test_every_authenticated_route_requires_the_token(self):
        body = self.issue()
        upload_id = body["upload_id"]
        self.use_cloudgene_response(ANONYMOUS)

        cases = (
            ("post", "/uploads", {"json": VALID_BODY}),
            ("post", f"/uploads/{upload_id}/complete", {}),
            ("get", f"/uploads/{upload_id}", {}),
        )
        for method, path, kwargs in cases:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 401)


class TestRequestValidation(AppTestCase):
    """§4.7: every validation failure is a 400 naming the failing check."""

    def test_size_over_the_maximum_is_400(self):
        response = self.post_upload(size=20 * 1024 * 1024)
        self.assertEqual(response.status_code, 400)
        self.assertIn("size", response.json()["detail"].lower())

    def test_zero_size_is_400(self):
        self.assertEqual(self.post_upload(size=0).status_code, 400)

    def test_negative_size_is_400(self):
        self.assertEqual(self.post_upload(size=-1).status_code, 400)

    def test_disallowed_content_type_is_400(self):
        response = self.post_upload(content_type="application/x-msdownload")
        self.assertEqual(response.status_code, 400)
        self.assertIn("content type", response.json()["detail"].lower())

    def test_content_type_parameters_are_tolerated(self):
        self.assertEqual(
            self.post_upload(
                content_type="text/plain; charset=utf-8",
                filename="notes.txt",
            ).status_code,
            201)

    def test_disallowed_extension_is_400(self):
        response = self.post_upload(filename="payload.exe")
        self.assertEqual(response.status_code, 400)
        self.assertIn("extension", response.json()["detail"].lower())

    def test_missing_fields_are_400_not_422(self):
        response = self.client.post(
            "/uploads",
            json={"filename": "reads.fastq.gz"},
            headers={"X-Auth-Token": "a.token.value"},
        )
        self.assertEqual(response.status_code, 400)

    def test_traversal_in_the_filename_is_400(self):
        for candidate in (
            "../../etc/passwd",
            "/etc/passwd",
            "..%2f..%2fescape.gz",
            "a\\b.gz",
        ):
            with self.subTest(filename=candidate):
                response = self.post_upload(filename=candidate)
                self.assertEqual(response.status_code, 400)

    def test_a_rejected_request_issues_no_sas_and_stores_no_record(self):
        self.post_upload(filename="../escape.gz")
        self.assertEqual(self.issuer.issued, [])
        self.assertEqual(self.store.list_for_owner(EMAIL), [])


class TestOwnerScoping(AppTestCase):
    """An upload ID that leaks must not become an authorisation bypass."""

    def test_user_b_cannot_read_user_a_upload(self):
        body = self.issue()

        other = dict(
            AUTHORISED,
            user=dict(AUTHORISED["user"], mail=OTHER_EMAIL))
        self.use_cloudgene_response(other)

        response = self.client.get(
            f"/uploads/{body['upload_id']}",
            headers={"X-Auth-Token": "another.token"})
        self.assertEqual(response.status_code, 404)

    def test_user_b_cannot_complete_user_a_upload(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        other = dict(
            AUTHORISED,
            user=dict(AUTHORISED["user"], mail=OTHER_EMAIL))
        self.use_cloudgene_response(other)

        response = self.client.post(
            f"/uploads/{body['upload_id']}/complete",
            headers={"X-Auth-Token": "another.token"})
        self.assertEqual(response.status_code, 404)

        # And the record is untouched.
        self.assertEqual(
            self.store.get(body["upload_id"], EMAIL).state,
            storage.STATE_PENDING)

    def test_unknown_upload_id_is_404(self):
        response = self.client.get(
            "/uploads/does-not-exist",
            headers={"X-Auth-Token": "a.token.value"})
        self.assertEqual(response.status_code, 404)


class TestCompletionAndReconciliation(AppTestCase):

    def complete(self, upload_id: str, payload: dict = None):
        """POST the completion callback."""
        return self.client.post(
            f"/uploads/{upload_id}/complete",
            json=payload or {},
            headers={"X-Auth-Token": "a.token.value"},
        )

    def test_matching_blob_completes(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        response = self.complete(body["upload_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], storage.STATE_COMPLETED)

    def test_missing_blob_fails(self):
        # §12: "client claims completion for a blob that was never written".
        body = self.issue()
        response = self.complete(body["upload_id"])
        self.assertEqual(response.json()["state"], storage.STATE_FAILED)
        self.assertIn("No blob", response.json()["detail"])

    def test_size_mismatch_fails(self):
        # §12: "client uploads 10x the declared size".
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"] * 10,
            content_type=VALID_BODY["content_type"])

        response = self.complete(body["upload_id"])
        self.assertEqual(response.json()["state"], storage.STATE_FAILED)
        self.assertIn("were declared", response.json()["detail"])

    def test_content_type_mismatch_fails(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type="application/x-msdownload")

        response = self.complete(body["upload_id"])
        self.assertEqual(response.json()["state"], storage.STATE_FAILED)

    def test_a_blob_with_no_content_type_still_completes(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"], content_type=None)
        self.assertEqual(
            self.complete(body["upload_id"]).json()["state"],
            storage.STATE_COMPLETED)

    def test_the_client_report_is_a_trigger_not_evidence(self):
        # Nothing in the body is trusted: the client insists it succeeded
        # and names a size that would match, but no blob exists.
        body = self.issue()
        response = self.complete(
            body["upload_id"],
            payload={
                "state": "completed",
                "size": VALID_BODY["size"],
                "success": True,
                "etag": "0x8D",
            },
        )
        self.assertEqual(response.json()["state"], storage.STATE_FAILED)

    def test_completion_is_idempotent(self):
        # §12: "Event Grid delivers twice" — idempotent validation, one
        # outcome.
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        first = self.complete(body["upload_id"]).json()
        second = self.complete(body["upload_id"]).json()

        self.assertEqual(first["state"], storage.STATE_COMPLETED)
        self.assertEqual(second["state"], storage.STATE_COMPLETED)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])

    def test_a_second_call_does_not_error(self):
        body = self.issue()
        self.complete(body["upload_id"])
        self.assertEqual(self.complete(body["upload_id"]).status_code, 200)

    def test_a_failed_verdict_is_not_overturned_by_a_later_call(self):
        body = self.issue()
        self.complete(body["upload_id"])

        # The blob appears afterwards; the verdict already stands.
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        second = self.complete(body["upload_id"]).json()
        self.assertEqual(second["state"], storage.STATE_FAILED)
        self.assertFalse(second["changed"])

    def test_reconcile_upload_is_callable_without_a_request(self):
        # Structured so an Event Grid webhook could call it unchanged.
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        record = self.store.get(body["upload_id"], EMAIL)
        resolved, changed = app_module.reconcile_upload(
            record, self.store, self.issuer)

        self.assertEqual(resolved.state, storage.STATE_COMPLETED)
        self.assertTrue(changed)

    def test_two_tokens_for_the_same_file_validate_independently(self):
        # §12: "two tokens issued for the same logical file".
        first = self.issue()
        second = self.issue()
        self.assertNotEqual(first["upload_id"], second["upload_id"])
        self.assertEqual(first["blob_path"], second["blob_path"])

        self.issuer.put_blob(
            first["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        self.assertEqual(
            self.complete(first["upload_id"]).json()["state"],
            storage.STATE_COMPLETED)
        self.assertEqual(
            self.complete(second["upload_id"]).json()["state"],
            storage.STATE_COMPLETED)


class TestExpirySweep(AppTestCase):

    def test_a_lapsed_pending_record_is_swept_on_the_next_issuance(self):
        # A client that never calls back leaves a 'pending' record behind.
        stale = self.store.create(
            user_email=EMAIL,
            blob_path=f"{EMAIL}/abandoned.gz",
            declared_size=1,
            declared_content_type="application/gzip",
            issued_at=storage.utcnow() - timedelta(days=3),
            expires_at=storage.utcnow() - timedelta(days=2),
        )

        self.issue()

        self.assertEqual(
            self.store.get(stale.upload_id, EMAIL).state,
            storage.STATE_EXPIRED)


class TestRateLimit(AppTestCase):

    rate_limit_max = 3

    def test_limit_trips_after_the_budget_is_spent(self):
        for _ in range(self.rate_limit_max):
            self.assertEqual(self.post_upload().status_code, 201)

        response = self.post_upload()
        self.assertEqual(response.status_code, 429)

    def test_429_carries_retry_after(self):
        for _ in range(self.rate_limit_max):
            self.post_upload()

        response = self.post_upload()
        self.assertIn("Retry-After", response.headers)
        self.assertGreater(int(response.headers["Retry-After"]), 0)

    def test_no_sas_is_issued_once_the_limit_trips(self):
        for _ in range(self.rate_limit_max):
            self.post_upload()
        issued_before = len(self.issuer.issued)

        self.post_upload()
        self.assertEqual(len(self.issuer.issued), issued_before)

    def test_the_limit_recovers_as_the_window_rolls(self):
        for _ in range(self.rate_limit_max):
            self.post_upload()
        self.assertEqual(self.post_upload().status_code, 429)

        # Age every issuance out of the rolling window.
        with self.store.session() as connection:
            connection.execute(
                "UPDATE uploads SET issued_at = ?",
                (storage.format_timestamp(
                    storage.utcnow() - timedelta(hours=2)),),
            )

        self.assertEqual(self.post_upload().status_code, 201)

    def test_the_budget_is_per_user(self):
        for _ in range(self.rate_limit_max):
            self.post_upload()
        self.assertEqual(self.post_upload().status_code, 429)

        other = dict(
            AUTHORISED,
            user=dict(AUTHORISED["user"], mail=OTHER_EMAIL))
        self.use_cloudgene_response(other)

        response = self.client.post(
            "/uploads",
            json=VALID_BODY,
            headers={"X-Auth-Token": "another.token"},
        )
        self.assertEqual(response.status_code, 201)


class TestAzPath(AppTestCase):

    def test_issuance_response_includes_az_path(self):
        body = self.issue()
        self.assertEqual(
            body["az_path"], f"az://{CONTAINER}/{body['blob_path']}")

    def test_status_read_includes_az_path(self):
        body = self.issue()
        response = self.client.get(
            f"/uploads/{body['upload_id']}",
            headers={"X-Auth-Token": "a.token.value"})
        self.assertEqual(
            response.json()["az_path"], f"az://{CONTAINER}/"
            f"{body['blob_path']}")

    def test_az_path_comes_from_config_not_the_client(self):
        # The client cannot influence the container named in az_path even
        # by trying to.
        body = self.issue(container="someone-elses-container")
        self.assertTrue(body["az_path"].startswith(f"az://{CONTAINER}/"))


class TestListFiles(AppTestCase):

    def list_files(self):
        return self.client.get(
            "/files", headers={"X-Auth-Token": "a.token.value"})

    def test_lists_only_the_callers_own_blobs(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        response = self.list_files()
        self.assertEqual(response.status_code, 200)
        paths = [f["blob_path"] for f in response.json()["files"]]
        self.assertEqual(paths, [body["blob_path"]])

    def test_bob_and_bob_dot_au_do_not_collide(self):
        # The trailing slash on the prefix is the security control.
        self.issuer.put_blob(
            "bob@example.com/secret.txt", size=1, content_type="text/plain")
        self.issuer.put_blob(
            "bob@example.com.au/other.txt", size=2,
            content_type="text/plain")

        other = dict(
            AUTHORISED,
            user=dict(AUTHORISED["user"], mail="bob@example.com"))
        self.use_cloudgene_response(other)

        response = self.list_files()
        paths = [f["blob_path"] for f in response.json()["files"]]
        self.assertEqual(paths, ["bob@example.com/secret.txt"])

    def test_a_blob_with_no_matching_record_is_listed_unannotated(self):
        self.issuer.put_blob(
            f"{EMAIL}/orphan.txt", size=5, content_type="text/plain")

        response = self.list_files()
        entry = response.json()["files"][0]
        self.assertEqual(entry["blob_path"], f"{EMAIL}/orphan.txt")
        self.assertNotIn("upload_id", entry)
        self.assertNotIn("state", entry)

    def test_a_record_whose_blob_is_gone_is_not_listed(self):
        self.issue()
        response = self.list_files()
        self.assertEqual(response.json()["files"], [])

    def test_a_blob_with_a_matching_record_is_annotated(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])
        self.complete(body["upload_id"])

        entry = self.list_files().json()["files"][0]
        self.assertEqual(entry["upload_id"], body["upload_id"])
        self.assertEqual(entry["state"], storage.STATE_COMPLETED)

    def complete(self, upload_id):
        return self.client.post(
            f"/uploads/{upload_id}/complete",
            headers={"X-Auth-Token": "a.token.value"})

    def test_no_query_parameter_can_move_the_prefix(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])
        self.issuer.put_blob(
            f"{OTHER_EMAIL}/other.txt", size=1, content_type="text/plain")

        for params in (
            {"prefix": f"{OTHER_EMAIL}/"},
            {"path": f"{OTHER_EMAIL}/"},
            {"user": OTHER_EMAIL},
        ):
            with self.subTest(params=params):
                response = self.client.get(
                    "/files", params=params,
                    headers={"X-Auth-Token": "a.token.value"})
                paths = [f["blob_path"] for f in response.json()["files"]]
                self.assertEqual(paths, [body["blob_path"]])

    def test_response_includes_az_path(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])

        entry = self.list_files().json()["files"][0]
        self.assertEqual(
            entry["az_path"], f"az://{CONTAINER}/{body['blob_path']}")

    def test_result_is_capped_and_reports_truncation(self):
        for i in range(5):
            self.issuer.put_blob(
                f"{EMAIL}/file{i}.txt", size=1, content_type="text/plain")

        self.app.state.config = dataclasses.replace(
            self.app.state.config, max_list_results=3)

        response = self.list_files()
        self.assertEqual(len(response.json()["files"]), 3)
        self.assertTrue(response.json()["truncated"])

    def test_result_is_not_truncated_under_the_cap(self):
        self.issuer.put_blob(
            f"{EMAIL}/file.txt", size=1, content_type="text/plain")
        response = self.list_files()
        self.assertFalse(response.json()["truncated"])

    def test_requires_authentication(self):
        self.use_cloudgene_response(ANONYMOUS)
        self.assertEqual(self.list_files().status_code, 401)


class TestRenewal(AppTestCase):

    def renew(self, upload_id, token="a.token.value"):
        return self.client.post(
            f"/uploads/{upload_id}/renew",
            headers={"X-Auth-Token": token})

    def test_renewal_returns_a_sas_for_the_same_blob_path(self):
        body = self.issue()
        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blob_path"], body["blob_path"])
        self.assertIn(body["blob_path"], response.json()["upload_url"])

    def test_renewal_extends_expires_at(self):
        body = self.issue()
        original_expiry = body["expires_at"]

        response = self.renew(body["upload_id"])

        self.assertGreater(
            response.json()["expires_at"], original_expiry)

    def test_a_renewed_record_survives_a_sweep(self):
        body = self.issue()
        self.renew(body["upload_id"])

        swept = self.store.expire_pending()
        self.assertEqual(swept, 0)
        self.assertEqual(
            self.store.get(body["upload_id"], EMAIL).state,
            storage.STATE_PENDING)

    def test_renewal_of_a_completed_upload_is_refused(self):
        body = self.issue()
        self.issuer.put_blob(
            body["blob_path"], size=VALID_BODY["size"],
            content_type=VALID_BODY["content_type"])
        self.client.post(
            f"/uploads/{body['upload_id']}/complete",
            headers={"X-Auth-Token": "a.token.value"})

        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 409)

    def test_renewal_of_a_failed_upload_is_refused(self):
        body = self.issue()
        # Never uploaded -> completion reconciles to failed.
        self.client.post(
            f"/uploads/{body['upload_id']}/complete",
            headers={"X-Auth-Token": "a.token.value"})

        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 409)

    def test_renewal_of_an_expired_upload_is_refused(self):
        body = self.issue()
        with self.store.session() as connection:
            connection.execute(
                "UPDATE uploads SET state = ?, expires_at = ? "
                "WHERE upload_id = ?",
                (
                    storage.STATE_EXPIRED,
                    storage.format_timestamp(
                        storage.utcnow() - timedelta(hours=1)),
                    body["upload_id"],
                ),
            )

        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 409)

    def test_renewal_of_another_users_upload_is_404_not_403(self):
        body = self.issue()

        other = dict(
            AUTHORISED,
            user=dict(AUTHORISED["user"], mail=OTHER_EMAIL))
        self.use_cloudgene_response(other)

        response = self.renew(body["upload_id"], token="another.token")
        self.assertEqual(response.status_code, 404)

    def test_the_renewal_cap_is_enforced(self):
        env = dict(os.environ, UPLOADER_MAX_RENEWALS="2")
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = app_module.create_app()
        with TestClient(app) as client:
            response = client.post(
                "/uploads", json=VALID_BODY,
                headers={"X-Auth-Token": "a.token.value"})
            upload_id = response.json()["upload_id"]

            for _ in range(2):
                response = client.post(
                    f"/uploads/{upload_id}/renew",
                    headers={"X-Auth-Token": "a.token.value"})
                self.assertEqual(response.status_code, 200)

            response = client.post(
                f"/uploads/{upload_id}/renew",
                headers={"X-Auth-Token": "a.token.value"})
            self.assertEqual(response.status_code, 429)

    def test_unknown_upload_id_is_404(self):
        response = self.renew("does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        body = self.issue()
        self.use_cloudgene_response(ANONYMOUS)
        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 401)

    def test_entitlement_is_rechecked_live(self):
        # Consistent with the completion callback: a user who loses access
        # mid-upload cannot renew.
        body = self.issue()
        self.use_cloudgene_response(UNENTITLED)
        response = self.renew(body["upload_id"])
        self.assertEqual(response.status_code, 403)


class TestStartup(unittest.TestCase):
    """A service that starts misconfigured is worse than one that won't."""

    def build(self, env: dict):
        """Return an app and its env patch, without entering the lifespan."""
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Never let a developer's real uploader/.env backfill a variable
        # this test is deliberately omitting.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dotenv_patcher = mock.patch.object(
            config_module, "DOTENV_PATH", Path(tmp.name) / "unused.env")
        dotenv_patcher.start()
        self.addCleanup(dotenv_patcher.stop)

        self_test = mock.patch.object(app_module, "startup_self_test")
        self_test.start()
        self.addCleanup(self_test.stop)

        return app_module.create_app()

    def test_missing_storage_account_refuses_to_start(self):
        application = self.build({"AZURE_STORAGE_CONTAINER": CONTAINER})
        with self.assertRaises(ConfigError):
            with TestClient(application):
                pass

    def test_missing_container_refuses_to_start(self):
        application = self.build({"AZURE_STORAGE_ACCOUNT": ACCOUNT})
        with self.assertRaises(ConfigError):
            with TestClient(application):
                pass

    def test_azure_issuer_without_a_certificate_refuses_to_start(self):
        application = self.build({
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
            "AZURE_TENANT_ID": "tenant",
            "AZURE_CLIENT_ID": "client",
        })
        with self.assertRaises(ConfigError):
            with TestClient(application):
                pass

    def test_azure_issuer_with_a_missing_certificate_refuses_to_start(self):
        """A path that does not exist is caught at startup, not on upload."""
        application = self.build({
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
            "AZURE_TENANT_ID": "tenant",
            "AZURE_CLIENT_ID": "client",
            "AZURE_CLIENT_CERTIFICATE_PATH": "/nonexistent/cert.pem",
        })
        with self.assertRaises(ConfigError):
            with TestClient(application):
                pass

    def test_an_unknown_issuer_name_refuses_to_start(self):
        application = self.build({
            "UPLOADER_SAS_ISSUER": "pretend",
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
        })
        with self.assertRaises(ConfigError):
            with TestClient(application):
                pass

    def test_a_failed_cloudgene_self_test_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = self.build({
                "UPLOADER_SAS_ISSUER": "fake",
                "AZURE_STORAGE_ACCOUNT": ACCOUNT,
                "AZURE_STORAGE_CONTAINER": CONTAINER,
                "UPLOADER_DB_PATH": str(Path(tmp) / "uploads.sqlite3"),
            })
            with mock.patch.object(
                app_module,
                "startup_self_test",
                side_effect=CloudgeneContractError("shape changed"),
            ):
                with self.assertRaises(CloudgeneContractError):
                    with TestClient(application):
                        pass

    def test_a_valid_configuration_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            application = self.build({
                "UPLOADER_SAS_ISSUER": "fake",
                "AZURE_STORAGE_ACCOUNT": ACCOUNT,
                "AZURE_STORAGE_CONTAINER": CONTAINER,
                "UPLOADER_DB_PATH": str(Path(tmp) / "uploads.sqlite3"),
            })
            with TestClient(application) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)


class TestRootPath(unittest.TestCase):

    def test_root_path_matches_the_nginx_prefix(self):
        # nginx's trailing slash on proxy_pass strips /uploads/api before
        # FastAPI sees the request (§6 of the spec); root_path is what puts
        # it back into generated URLs and the OpenAPI document.
        self.assertEqual(app_module.ROOT_PATH, "/uploads/api")
        self.assertEqual(app_module.app.root_path, "/uploads/api")


if __name__ == "__main__":
    unittest.main()
