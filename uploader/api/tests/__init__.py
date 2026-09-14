"""Test package for the uploader API.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

The service logs every auth decision and every issuance at INFO (§11 of the
spec), which is right in production and unreadable in a test run. The
loggers are quietened here rather than in the application, so nothing about
the production logging configuration is changed by the tests.
``assertLogs`` sets the level it needs for the duration of a block, so the
tests that assert on log output still work.
"""

import logging

for _name in ("uploader", "azure_sas", "httpx"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)
