"""Certificate handling in :mod:`config`.

The service principal authenticates with a certificate rather than a client
secret (``az ad sp create-for-rbac --create-cert``, see
``uploader/azure.md``). A certificate is a file, which introduces failure
modes a secret string does not have: it can be absent, unreadable, or left
world-readable on a shared host.

All three are checked at startup. The point is that a deployment mistake
surfaces as a service that will not start, naming the file, rather than as a
failed upload hours later reporting an Azure authentication error that
points nowhere near the filesystem.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from config import (
    ENV_CLIENT_CERT_PATH,
    ConfigError,
    load_config,
    redact,
)

ACCOUNT = "daffstandard"
CONTAINER = "uploads"
PEM_BODY = b"-----BEGIN PRIVATE KEY-----\nnot a real key\n"
MODE_OWNER_ONLY = 0o600
MODE_OWNER_GROUP_READ = 0o640
MODE_WORLD_READABLE = 0o644


def base_env(**overrides) -> dict:
    """Return a minimal environment for the real Azure issuer."""
    env = {
        "AZURE_STORAGE_ACCOUNT": ACCOUNT,
        "AZURE_STORAGE_CONTAINER": CONTAINER,
        "AZURE_TENANT_ID": "tenant",
        "AZURE_CLIENT_ID": "client",
    }
    env.update(overrides)
    return env


class CertificateTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cert = Path(self.tmp.name) / "azure-cert.pem"
        self.cert.write_bytes(PEM_BODY)
        self.cert.chmod(MODE_OWNER_ONLY)


class TestCertificateIsAccepted(CertificateTestCase):

    def test_a_readable_owner_only_certificate_loads(self):
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertEqual(config.client_certificate_path, self.cert)

    def test_owner_only_0600_is_allowed(self):
        """The deployed posture: www-data:www-data 0600, nobody else."""
        self.cert.chmod(MODE_OWNER_ONLY)
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertEqual(config.client_certificate_path, self.cert)

    def test_group_readable_is_allowed(self):
        """root:www-data 0640 is also valid, and is not a fault."""
        self.cert.chmod(MODE_OWNER_GROUP_READ)
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertEqual(config.client_certificate_path, self.cert)

    def test_the_path_is_expanded(self):
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertTrue(config.client_certificate_path.is_absolute())


class TestCertificateIsRejected(CertificateTestCase):

    def test_missing_variable_raises(self):
        with self.assertRaises(ConfigError):
            load_config(base_env())

    def test_empty_variable_raises(self):
        with self.assertRaises(ConfigError):
            load_config(base_env(**{ENV_CLIENT_CERT_PATH: "   "}))

    def test_nonexistent_path_raises(self):
        missing = Path(self.tmp.name) / "absent.pem"
        with self.assertRaises(ConfigError) as caught:
            load_config(base_env(**{ENV_CLIENT_CERT_PATH: str(missing)}))
        self.assertIn(str(missing), str(caught.exception))

    def test_a_directory_is_not_a_certificate(self):
        with self.assertRaises(ConfigError):
            load_config(base_env(**{ENV_CLIENT_CERT_PATH: self.tmp.name}))

    def test_world_readable_certificate_raises(self):
        """The PEM holds a private key; world-readable is a real exposure."""
        self.cert.chmod(MODE_WORLD_READABLE)
        with self.assertRaises(ConfigError) as caught:
            load_config(base_env(**{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertIn("world-readable", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_unreadable_certificate_raises(self):
        self.cert.chmod(0o200)
        with self.assertRaises(ConfigError):
            load_config(base_env(**{ENV_CLIENT_CERT_PATH: str(self.cert)}))

    def test_the_error_names_the_variable(self):
        """A deployment error must say which setting to fix."""
        with self.assertRaises(ConfigError) as caught:
            load_config(base_env(
                **{ENV_CLIENT_CERT_PATH: "/nonexistent/cert.pem"}))
        self.assertIn(ENV_CLIENT_CERT_PATH, str(caught.exception))


class TestFakeIssuerNeedsNoCertificate(CertificateTestCase):

    def test_fake_issuer_loads_without_any_azure_identity(self):
        config = load_config({
            "UPLOADER_SAS_ISSUER": "fake",
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
        })
        self.assertEqual(config.issuer, "fake")


class TestRedaction(CertificateTestCase):

    def test_redact_reports_the_path(self):
        """The path aids diagnosis and is not itself sensitive."""
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        self.assertEqual(
            redact(config)["client_certificate_path"], str(self.cert))

    def test_redact_never_carries_the_key_material(self):
        config = load_config(base_env(
            **{ENV_CLIENT_CERT_PATH: str(self.cert)}))
        rendered = repr(redact(config)).encode()
        self.assertNotIn(b"BEGIN PRIVATE KEY", rendered)


class TestPermissionConstants(unittest.TestCase):

    def test_world_readable_bit_is_what_we_think_it_is(self):
        self.assertTrue(MODE_WORLD_READABLE & stat.S_IROTH)
        self.assertFalse(MODE_OWNER_GROUP_READ & stat.S_IROTH)
        self.assertFalse(MODE_OWNER_ONLY & stat.S_IROTH)


if __name__ == "__main__":
    unittest.main()
