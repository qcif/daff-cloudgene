"""Loading configuration from an optional local ``.env`` file.

For local development, the variables :mod:`config` reads can come from a
``.env`` file at the uploader root instead of the real environment. Production
still relies on the systemd ``EnvironmentFile`` (§3 of
``uploader/spec/tasks/2_azure_resources.md``); this module only has to
prove that a ``.env`` file is picked up when present, and that a real
environment variable always wins over one from the file.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as config_module
from config import load_config

ACCOUNT = "daffstandard"
CONTAINER = "uploads"


class DotenvTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dotenv_path = Path(self.tmp.name) / ".env"

        patcher = mock.patch.object(
            config_module, "DOTENV_PATH", self.dotenv_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # A clean slate: only load_config's own call to load_dotenv() may
        # populate this, never whatever the test runner's shell exported.
        env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def write_dotenv(self, **values):
        lines = "\n".join(f"{k}={v}" for k, v in values.items())
        self.dotenv_path.write_text(lines + "\n")


class TestDotenvIsLoaded(DotenvTestCase):

    def test_values_from_the_dotenv_file_are_used(self):
        self.write_dotenv(
            UPLOADER_SAS_ISSUER="fake",
            AZURE_STORAGE_ACCOUNT=ACCOUNT,
            AZURE_STORAGE_CONTAINER=CONTAINER,
        )
        config = load_config()
        self.assertEqual(config.storage_account, ACCOUNT)
        self.assertEqual(config.storage_container, CONTAINER)

    def test_a_missing_dotenv_file_is_not_an_error(self):
        # DOTENV_PATH points at a file that was never written.
        os.environ["UPLOADER_SAS_ISSUER"] = "fake"
        os.environ["AZURE_STORAGE_ACCOUNT"] = ACCOUNT
        os.environ["AZURE_STORAGE_CONTAINER"] = CONTAINER
        config = load_config()
        self.assertEqual(config.storage_account, ACCOUNT)

    def test_a_real_environment_variable_wins_over_the_dotenv_file(self):
        self.write_dotenv(
            UPLOADER_SAS_ISSUER="fake",
            AZURE_STORAGE_ACCOUNT="from-dotenv",
            AZURE_STORAGE_CONTAINER=CONTAINER,
        )
        os.environ["AZURE_STORAGE_ACCOUNT"] = "from-real-environment"

        config = load_config()

        self.assertEqual(config.storage_account, "from-real-environment")

    def test_an_explicit_env_mapping_never_touches_the_dotenv_file(self):
        # The env= override exists for tests; it must not go anywhere near
        # disk, so this must succeed even though no .env file was written.
        config = load_config({
            "UPLOADER_SAS_ISSUER": "fake",
            "AZURE_STORAGE_ACCOUNT": ACCOUNT,
            "AZURE_STORAGE_CONTAINER": CONTAINER,
        })
        self.assertEqual(config.storage_account, ACCOUNT)
        self.assertFalse(self.dotenv_path.exists())


if __name__ == "__main__":
    unittest.main()
