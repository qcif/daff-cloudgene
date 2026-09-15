"""Config handling for the ``local`` SAS issuer.

Two settings, honoured only when ``UPLOADER_SAS_ISSUER=local`` (§7 of
``uploader/spec/tasks/06-mock-azure.md``): where blobs land on disk, and
the host embedded in issued URLs. Under every other issuer they must have
no effect at all — a stray root that does not exist yet must not block
startup of the azure or fake issuer.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from config import (
    ENV_LOCAL_BLOB_ENDPOINT,
    ENV_LOCAL_BLOB_ROOT,
    DEFAULT_LOCAL_BLOB_ENDPOINT,
    DEFAULT_LOCAL_BLOB_ROOT,
    ConfigError,
    ISSUER_AZURE,
    ISSUER_FAKE,
    ISSUER_LOCAL,
    load_config,
)

ACCOUNT = "daffstandard"
CONTAINER = "uploads"


def base_env(issuer: str, **overrides) -> dict:
    env = {
        "UPLOADER_SAS_ISSUER": issuer,
        "AZURE_STORAGE_ACCOUNT": ACCOUNT,
        "AZURE_STORAGE_CONTAINER": CONTAINER,
    }
    env.update(overrides)
    return env


class TestLocalIssuerAcceptsAWritableRoot(unittest.TestCase):

    def test_root_is_created_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-yet-created"
            config = load_config(base_env(
                ISSUER_LOCAL, **{ENV_LOCAL_BLOB_ROOT: str(root)}))
            self.assertTrue(root.is_dir())
            self.assertEqual(config.local_blob_root, root)

    def test_default_root_is_used_when_unset(self):
        config = load_config(base_env(ISSUER_LOCAL))
        self.assertEqual(
            config.local_blob_root, Path(DEFAULT_LOCAL_BLOB_ROOT))

    def test_default_endpoint_is_used_when_unset(self):
        config = load_config(base_env(ISSUER_LOCAL))
        self.assertEqual(
            config.local_blob_endpoint, DEFAULT_LOCAL_BLOB_ENDPOINT)

    def test_endpoint_is_honoured_when_set(self):
        config = load_config(base_env(
            ISSUER_LOCAL,
            **{ENV_LOCAL_BLOB_ENDPOINT: "http://127.0.0.1:9999"}))
        self.assertEqual(
            config.local_blob_endpoint, "http://127.0.0.1:9999")


class TestLocalIssuerRefusesAnUnwritableRoot(unittest.TestCase):
    """§7 of the task brief: fail fast at startup, not at first upload."""

    def test_a_file_where_a_directory_is_expected_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "not-a-directory"
            blocked.write_text("occupied")
            with self.assertRaises(ConfigError):
                load_config(base_env(
                    ISSUER_LOCAL, **{ENV_LOCAL_BLOB_ROOT: str(blocked)}))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_an_unwritable_root_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "readonly"
            root.mkdir()
            root.chmod(stat.S_IREAD | stat.S_IEXEC)
            try:
                with self.assertRaises(ConfigError):
                    load_config(base_env(
                        ISSUER_LOCAL, **{ENV_LOCAL_BLOB_ROOT: str(root)}))
            finally:
                # Restore write access so TemporaryDirectory can clean up.
                root.chmod(stat.S_IRWXU)


class TestLocalSettingsHaveNoEffectUnderOtherIssuers(unittest.TestCase):

    def test_a_nonexistent_root_does_not_block_the_fake_issuer(self):
        missing = "/nonexistent/path/for/uploader/tests"
        config = load_config(base_env(
            ISSUER_FAKE, **{ENV_LOCAL_BLOB_ROOT: missing}))
        self.assertEqual(config.issuer, ISSUER_FAKE)
        # The setting is parsed for the record, but never proven writable
        # when it belongs to an issuer that will never read it.
        self.assertFalse(Path(missing).exists())

    def test_a_nonexistent_root_does_not_block_the_azure_issuer(self):
        missing = "/nonexistent/path/for/uploader/tests"
        env = base_env(
            ISSUER_AZURE, **{ENV_LOCAL_BLOB_ROOT: missing})
        with tempfile.NamedTemporaryFile(suffix=".pem") as cert:
            env.update({
                "AZURE_TENANT_ID": "tenant",
                "AZURE_CLIENT_ID": "client",
                "AZURE_CLIENT_CERTIFICATE_PATH": cert.name,
            })
            Path(cert.name).chmod(0o600)
            config = load_config(env)
        self.assertEqual(config.issuer, ISSUER_AZURE)
        self.assertFalse(Path(missing).exists())


if __name__ == "__main__":
    unittest.main()
