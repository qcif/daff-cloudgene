"""Unit tests for cloudgene_auth.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

Fixtures
--------
``fixtures/anonymous.json`` is a **real** response body, recorded on
2026-09-14 from an anonymous ``GET`` of
``https://cloudgene.qcif.edu.au/api/v2/server``
(task brief §7.1/§7.2). A garbage token and a well-formed JWT with an invalid
signature both produced a byte-identical body, so the same fixture covers
§7.4 and the synthetic §7.5 case.

``fixtures/authorised.json`` and ``fixtures/unentitled.json`` are also **real**
bodies, recorded 2026-09-14 from the same account either side of a change to
its group membership:

- ``authorised.json`` — roles include ``daff-wfs``; ``apps`` holds
  ``taxodactyl_150@1.5.0``.
- ``unentitled.json`` — the same user with the role removed; ``loggedIn`` is
  still true, ``apps`` is ``[]``. Recorded over loopback by the operator.

Note that ``apps`` reflects the account's **live** entitlement at request time,
not the ``roles`` claim baked into the token — the same token string produced
both bodies. This is why authorisation must never be read from the JWT.

Bodies constructed inside individual tests (missing fields, wrong types) are
synthetic by design: they model contract violations that cannot be produced on
demand from a healthy server.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import requests

import cloudgene_auth
from cloudgene_auth import (
    CloudgeneAuthError,
    CloudgeneContractError,
    CloudgeneUnavailableError,
    NoUserEmailError,
    NotAuthenticatedError,
    fetch_server_info,
    interpret_server_info,
    startup_self_test,
    validate_token,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ANONYMOUS_FIXTURE = FIXTURES_DIR / "anonymous.json"
AUTHORISED_FIXTURE = FIXTURES_DIR / "authorised.json"
UNENTITLED_FIXTURE = FIXTURES_DIR / "unentitled.json"


def load_fixture(path: Path) -> dict:
    """Return a recorded ``/api/v2/server`` response body."""
    with path.open() as f:
        return json.load(f)


def load_anonymous() -> dict:
    """Return the recorded anonymous response body."""
    return load_fixture(ANONYMOUS_FIXTURE)


RECORDED_AUTHORISED = load_fixture(AUTHORISED_FIXTURE)
RECORDED_UNENTITLED = load_fixture(UNENTITLED_FIXTURE)
RECORDED_EMAIL = RECORDED_AUTHORISED["user"]["mail"].lower()


def fake_response(
    status_code: int = 200,
    payload: object = None,
    text: str = None,
) -> mock.Mock:
    """Build a stand-in for a ``requests.Response``."""
    response = mock.Mock()
    response.status_code = status_code
    if text is not None:
        response.json.side_effect = ValueError("not JSON")
        response.text = text
    else:
        response.json.return_value = payload
    return response


class TestRecordedAnonymousFixture(unittest.TestCase):
    """The observed §7.1/§7.2 response must be treated as logged out."""

    def test_fixture_shape_matches_what_was_observed(self):
        data = load_anonymous()
        self.assertIs(data["loggedIn"], False)
        self.assertEqual(data["apps"], [])
        # Observed: the anonymous body carries no 'user' key at all.
        self.assertNotIn("user", data)

    def test_anonymous_body_raises_not_authenticated(self):
        with self.assertRaises(NotAuthenticatedError):
            interpret_server_info(load_anonymous())

    def test_garbage_token_response_is_the_same_body(self):
        # Observed: `X-Auth-Token: not-a-token` returned HTTP 200 with an
        # identical anonymous body (§7.4).
        with self.assertRaises(NotAuthenticatedError):
            interpret_server_info(load_anonymous())


class TestInterpretServerInfo(unittest.TestCase):

    def test_authorised_user(self):
        email, authorised = interpret_server_info(RECORDED_AUTHORISED)
        self.assertEqual(email, RECORDED_EMAIL)
        self.assertTrue(authorised)

    def test_email_is_lowercased(self):
        email, _ = interpret_server_info(RECORDED_AUTHORISED)
        self.assertEqual(email, email.lower())

    def test_logged_in_but_no_apps_is_denied_not_raised(self):
        email, authorised = interpret_server_info(RECORDED_UNENTITLED)
        self.assertEqual(email, RECORDED_EMAIL)
        self.assertFalse(authorised)

    def test_deprecated_and_experimental_apps_do_not_authorise(self):
        """Only 'apps' counts — decided by the operator, 2026-09-14.

        A user entitled solely to a deprecated or experimental app is denied.
        This test exists to make that denial deliberate rather than incidental:
        summing the three lists would look like a reasonable bug fix.
        """
        data = dict(
            RECORDED_UNENTITLED,
            deprecatedApps=[{"id": "taxodactyl_old"}],
            experimentalApps=[{"id": "taxodactyl_dev"}],
        )
        _, authorised = interpret_server_info(data)
        self.assertFalse(authorised)

    def test_missing_logged_in_raises(self):
        data = dict(RECORDED_AUTHORISED)
        data.pop("loggedIn")
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_truthy_non_boolean_logged_in_raises(self):
        # "true" is truthy in Python; it must not be read as authenticated.
        data = dict(RECORDED_AUTHORISED, loggedIn="true")
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_missing_user_object_raises(self):
        data = dict(RECORDED_AUTHORISED)
        data.pop("user")
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_missing_user_mail_raises(self):
        data = dict(RECORDED_AUTHORISED, user={"username": "someuser"})
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_blank_user_mail_raises_no_email_not_contract_error(self):
        # An account with no email is a legitimate Cloudgene state, not a
        # broken endpoint — it must not be reported as an outage.
        data = dict(RECORDED_AUTHORISED, user={"mail": "   "})
        with self.assertRaises(NoUserEmailError):
            interpret_server_info(data)

    def test_null_user_mail_raises_no_email(self):
        data = dict(RECORDED_AUTHORISED, user={"mail": None})
        with self.assertRaises(NoUserEmailError):
            interpret_server_info(data)

    def test_no_email_error_message_tells_the_user_what_to_fix(self):
        data = dict(RECORDED_AUTHORISED, user={"mail": None})
        with self.assertRaises(NoUserEmailError) as ctx:
            interpret_server_info(data)
        self.assertIn("no email address", str(ctx.exception))

    def test_no_email_still_fails_closed_under_a_catch_all(self):
        data = dict(RECORDED_AUTHORISED, user={"mail": None})
        with self.assertRaises(CloudgeneAuthError):
            interpret_server_info(data)

    def test_non_string_user_mail_raises_contract_error(self):
        # A number or object here is a genuine contract violation.
        data = dict(RECORDED_AUTHORISED, user={"mail": 12345})
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_missing_apps_raises(self):
        data = dict(RECORDED_AUTHORISED)
        data.pop("apps")
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)

    def test_apps_not_a_list_raises(self):
        data = dict(RECORDED_AUTHORISED, apps={"id": "taxodactyl"})
        with self.assertRaises(CloudgeneContractError):
            interpret_server_info(data)


class TestTokenSanity(unittest.TestCase):
    """Tokens that cannot be sent are rejected before any network call."""

    def test_empty_token_raises(self):
        for value in ("", "   ", None, 12345):
            with self.subTest(value=value):
                with self.assertRaises(NotAuthenticatedError):
                    validate_token(value)

    def test_header_injection_attempt_raises(self):
        with self.assertRaises(NotAuthenticatedError):
            validate_token("abc\r\nX-Admin: true")

    def test_over_long_token_raises(self):
        with self.assertRaises(NotAuthenticatedError):
            validate_token("a" * 100000)

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_no_request_is_made_for_an_empty_token(self, mock_get):
        with self.assertRaises(NotAuthenticatedError):
            validate_token("")
        mock_get.assert_not_called()


class TestFetchServerInfo(unittest.TestCase):

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_token_is_forwarded_verbatim_in_the_header(self, mock_get):
        mock_get.return_value = fake_response(payload=RECORDED_AUTHORISED)
        fetch_server_info("  a.token.value  ")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["headers"]["X-Auth-Token"], "a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_default_base_url_is_the_public_host(self, mock_get):
        mock_get.return_value = fake_response(payload=RECORDED_AUTHORISED)
        with mock.patch.dict(cloudgene_auth.os.environ, {}, clear=True):
            fetch_server_info("a.token.value")
        args, _ = mock_get.call_args
        self.assertEqual(
            args[0],
            "https://cloudgene.qcif.edu.au/api/v2/server")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_base_url_can_be_switched_to_loopback(self, mock_get):
        mock_get.return_value = fake_response(payload=RECORDED_AUTHORISED)
        env = {
            cloudgene_auth.CLOUDGENE_BASE_URL_ENV_VAR:
                cloudgene_auth.CLOUDGENE_LOOPBACK_BASE_URL,
        }
        with mock.patch.dict(cloudgene_auth.os.environ, env, clear=True):
            fetch_server_info("a.token.value")
        args, _ = mock_get.call_args
        self.assertEqual(args[0], "http://127.0.0.1:8082/api/v2/server")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_connection_error_raises_unavailable(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(CloudgeneUnavailableError):
            fetch_server_info("a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_timeout_raises_unavailable(self, mock_get):
        mock_get.side_effect = requests.Timeout("timed out")
        with self.assertRaises(CloudgeneUnavailableError):
            fetch_server_info("a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_non_200_raises_unavailable(self, mock_get):
        for status in (401, 403, 404, 500, 502, 503):
            with self.subTest(status=status):
                mock_get.return_value = fake_response(
                    status_code=status, payload={})
                with self.assertRaises(CloudgeneUnavailableError):
                    fetch_server_info("a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_html_body_raises_unavailable(self, mock_get):
        # What an nginx error page or a maintenance splash would look like.
        mock_get.return_value = fake_response(text="<html>502</html>")
        with self.assertRaises(CloudgeneUnavailableError):
            fetch_server_info("a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_json_array_body_raises_unavailable(self, mock_get):
        mock_get.return_value = fake_response(payload=[1, 2, 3])
        with self.assertRaises(CloudgeneUnavailableError):
            fetch_server_info("a.token.value")


class TestValidateTokenEndToEnd(unittest.TestCase):
    """The public entry point, with the network mocked out."""

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_authorised(self, mock_get):
        mock_get.return_value = fake_response(payload=RECORDED_AUTHORISED)
        self.assertEqual(
            validate_token("a.token.value"),
            (RECORDED_EMAIL, True))

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_unentitled(self, mock_get):
        mock_get.return_value = fake_response(payload=RECORDED_UNENTITLED)
        self.assertEqual(
            validate_token("a.token.value"),
            (RECORDED_EMAIL, False))

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_expired_or_forged_token(self, mock_get):
        # Observed: Cloudgene answers 200 with the anonymous body.
        mock_get.return_value = fake_response(payload=load_anonymous())
        with self.assertRaises(NotAuthenticatedError):
            validate_token("a.token.value")

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_never_returns_a_value_on_any_error_path(self, mock_get):
        failures = [
            requests.ConnectionError("refused"),
            requests.Timeout("timed out"),
            requests.TooManyRedirects("loop"),
        ]
        for exc in failures:
            with self.subTest(exc=exc.__class__.__name__):
                mock_get.side_effect = exc
                with self.assertRaises(cloudgene_auth.CloudgeneAuthError):
                    validate_token("a.token.value")


class TestStartupSelfTest(unittest.TestCase):

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_passes_against_the_recorded_anonymous_response(self, mock_get):
        mock_get.return_value = fake_response(payload=load_anonymous())
        startup_self_test()

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_fails_if_logged_in_field_disappears(self, mock_get):
        data = load_anonymous()
        data.pop("loggedIn")
        mock_get.return_value = fake_response(payload=data)
        with self.assertRaises(CloudgeneContractError):
            startup_self_test()

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_fails_if_apps_field_disappears(self, mock_get):
        data = load_anonymous()
        data.pop("apps")
        mock_get.return_value = fake_response(payload=data)
        with self.assertRaises(CloudgeneContractError):
            startup_self_test()

    @mock.patch.object(cloudgene_auth.requests, "get")
    def test_fails_if_cloudgene_is_down(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(CloudgeneUnavailableError):
            startup_self_test()


if __name__ == "__main__":
    unittest.main()
