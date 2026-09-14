"""Unit tests for azure_sas.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

**No test here reaches Azure.** No service principal exists yet (§3 of
``uploader/spec/tasks/2_azure_resources.md``), so
:class:`AzureUserDelegationSasIssuer` is exercised with the SDK's network
calls mocked out. What that does cover is the part most worth covering:
the SAS parameters. ``c`` versus ``w`` is the difference between "may
create this one blob once" and "may overwrite anything it names", so a
future edit that widens the permission should break a test loudly rather
than ship.

What it does **not** cover is listed in the task report: whether Entra
accepts the credential, whether the role assignment is sufficient for
``generateUserDelegationKey``, and whether Azure accepts the resulting
signature.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions

import azure_sas
from azure_sas import (
    DELEGATION_KEY_LIFETIME,
    DELEGATION_KEY_REFRESH_MARGIN,
    SAS_PROTOCOL_HTTPS,
    SAS_START_SKEW,
    AzureUserDelegationSasIssuer,
    FakeSasIssuer,
    IssuedSas,
    SasError,
    create_issuer,
    sas_start_time,
)
from config import Config, ISSUER_AZURE, ISSUER_FAKE
from pathlib import Path

ACCOUNT = "daffstandard"
CONTAINER = "uploads"
BLOB_PATH = "chyde@neoformit.com/run7/reads.fastq.gz"
SIGNED_TOKEN = "sv=2024-11-04&sr=b&sp=c&sig=abc"


def make_config(issuer: str) -> Config:
    """Return a config with only the fields this module reads."""
    return Config(
        issuer=issuer,
        storage_account=ACCOUNT,
        storage_container=CONTAINER,
        tenant_id="tenant",
        client_id="client",
        client_certificate_path=Path("/etc/uploads/cert.pem"),
        database_path=Path("uploads.sqlite3"),
        max_upload_bytes=1,
        sas_ttl_seconds=1,
        rate_limit_max=1,
        rate_limit_window_seconds=1,
        allowed_extensions=frozenset({".gz"}),
        allowed_content_types=frozenset({"application/gzip"}),
        max_list_results=1000,
        max_renewals=10,
    )


def expiry_in(hours: int = 24) -> datetime:
    """Return an expiry ``hours`` from now."""
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class TestPermissionConstant(unittest.TestCase):
    """The single most load-bearing character in the whole service."""

    def test_create_permission_renders_as_c(self):
        self.assertEqual(str(BlobSasPermissions(create=True)), "c")

    def test_write_permission_is_a_different_character(self):
        # Guards the assumption behind the test above: if 'c' and 'w' were
        # the same string, asserting on 'c' would prove nothing.
        self.assertNotEqual(
            str(BlobSasPermissions(create=True)),
            str(BlobSasPermissions(write=True)))


class TestSasStartTime(unittest.TestCase):

    def test_start_is_five_minutes_in_the_past(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(sas_start_time(now), now - SAS_START_SKEW)
        self.assertEqual(SAS_START_SKEW, timedelta(minutes=5))


class TestIssuedSas(unittest.TestCase):

    def test_url_joins_the_blob_url_and_the_token(self):
        issued = IssuedSas(
            blob_url="https://x.blob.core.windows.net/uploads/a.txt",
            sas_token="sp=c&sr=b",
            expires_at=expiry_in(),
        )
        self.assertEqual(
            issued.url,
            "https://x.blob.core.windows.net/uploads/a.txt?sp=c&sr=b")


class TestFakeSasIssuer(unittest.TestCase):

    def setUp(self):
        self.issuer = FakeSasIssuer(ACCOUNT, CONTAINER)

    def query(self, token: str) -> dict:
        """Return the SAS query string as a flat dict."""
        return {k: v[0] for k, v in parse_qs(token).items()}

    def test_permission_is_exactly_create(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(self.query(issued.sas_token)["sp"], "c")

    def test_resource_is_a_single_blob(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(self.query(issued.sas_token)["sr"], "b")

    def test_protocol_is_https_only(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(
            self.query(issued.sas_token)["spr"], SAS_PROTOCOL_HTTPS)

    def test_no_ip_range_is_set(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertNotIn("sip", self.query(issued.sas_token))

    def test_expiry_is_honoured(self):
        expiry = expiry_in(24)
        issued = self.issuer.issue(BLOB_PATH, expiry)
        self.assertEqual(issued.expires_at, expiry)
        self.assertEqual(
            self.query(issued.sas_token)["se"],
            expiry.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_start_time_precedes_now(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        start = datetime.strptime(
            self.query(issued.sas_token)["st"],
            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(start, datetime.now(timezone.utc))

    def test_url_points_at_the_configured_account_and_container(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertTrue(issued.blob_url.startswith(
            f"https://{ACCOUNT}.blob.core.windows.net/{CONTAINER}/"))
        self.assertTrue(issued.blob_url.endswith(BLOB_PATH))

    def test_unknown_blob_has_no_properties(self):
        self.assertIsNone(self.issuer.get_blob_properties(BLOB_PATH))

    def test_put_blob_then_read_properties(self):
        self.issuer.put_blob(BLOB_PATH, size=1234, content_type="text/plain")
        properties = self.issuer.get_blob_properties(BLOB_PATH)
        self.assertEqual(properties.size, 1234)
        self.assertEqual(properties.content_type, "text/plain")

    def test_list_blobs_returns_only_matching_prefix(self):
        self.issuer.put_blob(
            "alice@example.com/a.txt", size=1, content_type="text/plain")
        self.issuer.put_blob(
            "bob@example.com/b.txt", size=2, content_type="text/plain")

        listed = self.issuer.list_blobs("alice@example.com/")

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].blob_path, "alice@example.com/a.txt")
        self.assertEqual(listed[0].size, 1)

    def test_list_blobs_prefix_does_not_match_a_similar_email(self):
        # bob@example.com must not match bob@example.com.au/...: this is
        # the whole reason naming.prefix_for adds a trailing slash.
        self.issuer.put_blob(
            "bob@example.com/secret.txt", size=1, content_type="text/plain")
        self.issuer.put_blob(
            "bob@example.com.au/other.txt", size=2,
            content_type="text/plain")

        listed = self.issuer.list_blobs("bob@example.com/")

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].blob_path, "bob@example.com/secret.txt")

    def test_list_blobs_empty_prefix_matches_nothing_unknown(self):
        self.assertEqual(self.issuer.list_blobs("nobody@example.com/"), [])

    def test_list_blobs_includes_last_modified(self):
        self.issuer.put_blob(BLOB_PATH, size=1, content_type="text/plain")
        listed = self.issuer.list_blobs("chyde@neoformit.com/")
        self.assertIsNotNone(listed[0].last_modified)


class AzureIssuerTestCase(unittest.TestCase):
    """Base class wiring a mocked BlobServiceClient into the real issuer."""

    def setUp(self):
        self.client = mock.Mock()
        self.key = mock.Mock(name="UserDelegationKey")
        self.client.get_user_delegation_key.return_value = self.key
        self.issuer = AzureUserDelegationSasIssuer(
            account_name=ACCOUNT,
            container_name=CONTAINER,
            tenant_id="tenant",
            client_id="client",
            client_certificate_path=Path("/etc/uploads/cert.pem"),
            blob_service_client=self.client,
        )

        patcher = mock.patch.object(
            azure_sas, "generate_blob_sas", return_value=SIGNED_TOKEN)
        self.generate = patcher.start()
        self.addCleanup(patcher.stop)


class TestAzureIssuerParameters(AzureIssuerTestCase):
    """§7 of the spec, parameter by parameter."""

    def test_permission_is_exactly_create(self):
        self.issuer.issue(BLOB_PATH, expiry_in())
        permission = self.generate.call_args.kwargs["permission"]
        self.assertEqual(str(permission), "c")
        self.assertTrue(permission.create)
        self.assertFalse(permission.write)
        self.assertFalse(permission.delete)
        self.assertFalse(permission.read)
        self.assertFalse(permission.add)

    def test_resource_is_a_single_blob_not_a_container(self):
        # generate_blob_sas is what produces sr=b; generate_container_sas
        # would produce sr=c and grant write to every blob in it.
        self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(
            self.generate.call_args.kwargs["blob_name"], BLOB_PATH)
        self.assertEqual(
            self.generate.call_args.kwargs["container_name"], CONTAINER)
        self.assertEqual(
            self.generate.call_args.kwargs["account_name"], ACCOUNT)

    def test_expiry_is_passed_through_unchanged(self):
        expiry = expiry_in(24)
        issued = self.issuer.issue(BLOB_PATH, expiry)
        self.assertEqual(self.generate.call_args.kwargs["expiry"], expiry)
        self.assertEqual(issued.expires_at, expiry)

    def test_start_is_five_minutes_in_the_past(self):
        before = datetime.now(timezone.utc)
        self.issuer.issue(BLOB_PATH, expiry_in())
        start = self.generate.call_args.kwargs["start"]
        self.assertLessEqual(start, before - SAS_START_SKEW + timedelta(
            seconds=1))
        self.assertGreater(start, before - SAS_START_SKEW - timedelta(
            seconds=10))

    def test_protocol_is_https_only(self):
        self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(
            self.generate.call_args.kwargs["protocol"], SAS_PROTOCOL_HTTPS)

    def test_no_ip_range_is_supplied(self):
        self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertIsNone(self.generate.call_args.kwargs.get("ip"))

    def test_no_account_key_is_used(self):
        # A user delegation SAS, not an account-key SAS (§4 of the spec).
        self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertIsNone(
            self.generate.call_args.kwargs.get("account_key"))
        self.assertIs(
            self.generate.call_args.kwargs["user_delegation_key"], self.key)

    def test_returned_url_carries_the_token(self):
        issued = self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(
            issued.url,
            f"https://{ACCOUNT}.blob.core.windows.net/{CONTAINER}/"
            f"{BLOB_PATH}?{SIGNED_TOKEN}")


class TestDelegationKeyCaching(AzureIssuerTestCase):

    def test_key_is_requested_once_and_reused(self):
        for _ in range(5):
            self.issuer.issue(BLOB_PATH, expiry_in())
        self.client.get_user_delegation_key.assert_called_once()

    def test_requested_lifetime_is_within_the_seven_day_maximum(self):
        self.issuer.issue(BLOB_PATH, expiry_in())
        kwargs = self.client.get_user_delegation_key.call_args.kwargs
        lifetime = kwargs["key_expiry_time"] - kwargs["key_start_time"]
        self.assertLess(lifetime, timedelta(days=7))
        self.assertEqual(
            DELEGATION_KEY_LIFETIME + SAS_START_SKEW, lifetime)

    def test_key_is_refreshed_before_it_expires(self):
        self.issuer.issue(BLOB_PATH, expiry_in())
        self.client.get_user_delegation_key.assert_called_once()

        # Wind the cached expiry to just inside the refresh margin.
        self.issuer._delegation_key_expiry = (
            datetime.now(timezone.utc)
            + DELEGATION_KEY_REFRESH_MARGIN
            - timedelta(minutes=1))

        self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertEqual(self.client.get_user_delegation_key.call_count, 2)

    def test_a_failure_to_obtain_a_key_is_a_sas_error(self):
        from azure.core.exceptions import ClientAuthenticationError
        self.client.get_user_delegation_key.side_effect = (
            ClientAuthenticationError("bad secret"))
        with self.assertRaises(SasError):
            self.issuer.issue(BLOB_PATH, expiry_in())


class TestStaleKeyRetry(AzureIssuerTestCase):
    """Exactly one retry, per §4 of the spec — not a loop."""

    def test_signing_failure_refreshes_the_key_and_retries_once(self):
        self.generate.side_effect = [RuntimeError("stale key"), SIGNED_TOKEN]
        issued = self.issuer.issue(BLOB_PATH, expiry_in())

        self.assertEqual(issued.sas_token, SIGNED_TOKEN)
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.client.get_user_delegation_key.call_count, 2)

    def test_a_second_failure_raises_rather_than_looping(self):
        self.generate.side_effect = RuntimeError("still broken")
        with self.assertRaises(SasError):
            self.issuer.issue(BLOB_PATH, expiry_in())

        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.client.get_user_delegation_key.call_count, 2)

    def test_the_raised_error_carries_only_the_exception_class_name(self):
        # The underlying exception's message may quote a credential or a
        # signature; only its type is safe to propagate.
        self.generate.side_effect = RuntimeError(
            "sig=AAAAtopsecretsignature")
        with self.assertRaises(SasError) as ctx:
            self.issuer.issue(BLOB_PATH, expiry_in())
        self.assertNotIn("topsecret", str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))


class TestAzureIssuerBlobProperties(AzureIssuerTestCase):

    def _blob_client(self):
        blob_client = mock.Mock()
        self.client.get_blob_client.return_value = blob_client
        return blob_client

    def test_missing_blob_returns_none(self):
        self._blob_client().get_blob_properties.side_effect = (
            ResourceNotFoundError("not there"))
        self.assertIsNone(self.issuer.get_blob_properties(BLOB_PATH))

    def test_properties_are_read_from_the_blob_not_the_client_report(self):
        properties = mock.Mock()
        properties.size = 4096
        properties.content_settings = mock.Mock(content_type="text/plain")
        self._blob_client().get_blob_properties.return_value = properties

        result = self.issuer.get_blob_properties(BLOB_PATH)
        self.assertEqual(result.size, 4096)
        self.assertEqual(result.content_type, "text/plain")
        self.client.get_blob_client.assert_called_once_with(
            container=CONTAINER, blob=BLOB_PATH)

    def test_an_azure_failure_is_a_sas_error(self):
        from azure.core.exceptions import ServiceRequestError
        self._blob_client().get_blob_properties.side_effect = (
            ServiceRequestError("no route"))
        with self.assertRaises(SasError):
            self.issuer.get_blob_properties(BLOB_PATH)


class TestAzureIssuerListBlobs(AzureIssuerTestCase):

    def _blob(self, name, size, content_type=None):
        blob = mock.Mock()
        blob.name = name
        blob.size = size
        blob.content_settings = mock.Mock(content_type=content_type)
        blob.last_modified = datetime.now(timezone.utc)
        return blob

    def test_lists_via_the_container_client_with_the_prefix(self):
        container_client = mock.Mock()
        container_client.list_blobs.return_value = [
            self._blob("chyde@neoformit.com/a.txt", 1, "text/plain"),
        ]
        self.client.get_container_client.return_value = container_client

        listed = self.issuer.list_blobs("chyde@neoformit.com/")

        self.client.get_container_client.assert_called_once_with(CONTAINER)
        container_client.list_blobs.assert_called_once_with(
            name_starts_with="chyde@neoformit.com/")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].blob_path, "chyde@neoformit.com/a.txt")
        self.assertEqual(listed[0].size, 1)
        self.assertEqual(listed[0].content_type, "text/plain")

    def test_an_azure_failure_while_listing_is_a_sas_error(self):
        from azure.core.exceptions import ServiceRequestError
        container_client = mock.Mock()
        container_client.list_blobs.side_effect = ServiceRequestError(
            "no route")
        self.client.get_container_client.return_value = container_client

        with self.assertRaises(SasError):
            self.issuer.list_blobs("chyde@neoformit.com/")


class TestCreateIssuer(unittest.TestCase):

    def test_fake_is_selected_only_when_asked_for(self):
        issuer = create_issuer(make_config(ISSUER_FAKE))
        self.assertIsInstance(issuer, FakeSasIssuer)

    def test_azure_is_the_default_selection(self):
        issuer = create_issuer(make_config(ISSUER_AZURE))
        self.assertIsInstance(issuer, AzureUserDelegationSasIssuer)

    def test_unknown_issuer_fails_closed(self):
        with self.assertRaises(SasError):
            create_issuer(make_config("something-else"))

    def test_azure_issuer_builds_no_credential_until_first_use(self):
        # Constructing it must not touch the network, so a misconfigured
        # secret surfaces on the first request rather than at import.
        issuer = create_issuer(make_config(ISSUER_AZURE))
        self.assertIsNone(issuer._client)


if __name__ == "__main__":
    unittest.main()
