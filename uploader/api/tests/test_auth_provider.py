"""Unit tests for the fake auth provider used in local development.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

The fake exists so the UI can be exercised without a real Cloudgene session
(see ``../README.md``). That makes it a piece of security-adjacent code with
no user in front of it, so the properties worth pinning down are the ones
that would make it *dangerous* rather than the ones that make it convenient:
that it is never the default, that an unknown token is not a free pass, and
that it cannot be combined with a real SAS issuer.
"""

import os
import unittest
from unittest import mock

import cloudgene_auth
from cloudgene_auth import (
    AUTH_PROVIDER_CLOUDGENE,
    AUTH_PROVIDER_ENV_VAR,
    AUTH_PROVIDER_FAKE,
    AuthProviderError,
    FAKE_TOKEN_NO_EMAIL,
    NoUserEmailError,
    NotAuthenticatedError,
    get_auth_provider,
    validate_token,
)

AUTHORISED_EMAIL = "chyde@neoformit.com"


def use_provider(value):
    """Return a context manager selecting an auth provider."""
    return mock.patch.dict(os.environ, {AUTH_PROVIDER_ENV_VAR: value})


class TestGetAuthProvider(unittest.TestCase):

    def test_defaults_to_the_real_cloudgene(self):
        # The important half of the default: a host that forgets the
        # variable validates against Cloudgene, it does not fall back to a
        # table of canned answers.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_auth_provider(), AUTH_PROVIDER_CLOUDGENE)

    def test_fake_must_be_opted_into_explicitly(self):
        with use_provider(AUTH_PROVIDER_FAKE):
            self.assertEqual(get_auth_provider(), AUTH_PROVIDER_FAKE)

    def test_is_case_and_whitespace_insensitive(self):
        with use_provider("  FAKE  "):
            self.assertEqual(get_auth_provider(), AUTH_PROVIDER_FAKE)

    def test_rejects_an_unknown_provider(self):
        for value in ("cloudgene-ish", "none", "off", "true"):
            with self.subTest(value=value):
                with use_provider(value):
                    with self.assertRaises(AuthProviderError):
                        get_auth_provider()

    def test_an_empty_value_is_the_real_provider_not_an_error(self):
        # An EnvironmentFile line like `UPLOADER_AUTH_PROVIDER=` should not
        # stop the service; it should mean "unset".
        with use_provider(""):
            self.assertEqual(get_auth_provider(), AUTH_PROVIDER_CLOUDGENE)


class TestFakeProviderNeverTouchesTheNetwork(unittest.TestCase):

    def test_validate_token_makes_no_request(self):
        with use_provider(AUTH_PROVIDER_FAKE):
            with mock.patch.object(cloudgene_auth, "requests") as requests:
                validate_token("dev-authorised")
        requests.get.assert_not_called()

    def test_startup_self_test_makes_no_request(self):
        with use_provider(AUTH_PROVIDER_FAKE):
            with mock.patch.object(cloudgene_auth, "requests") as requests:
                cloudgene_auth.startup_self_test()
        requests.get.assert_not_called()


class TestFakeTokens(unittest.TestCase):
    """Each dev token must reach the real decision logic, not bypass it."""

    def test_authorised_token_resolves_to_an_entitled_user(self):
        with use_provider(AUTH_PROVIDER_FAKE):
            email, is_authorised = validate_token("dev-authorised")

        self.assertEqual(email, AUTHORISED_EMAIL)
        self.assertTrue(is_authorised)

    def test_unentitled_token_is_logged_in_but_not_authorised(self):
        # The one non-error denial: a real session with no workflow access.
        # It must not look like a failed login.
        with use_provider(AUTH_PROVIDER_FAKE):
            email, is_authorised = validate_token("dev-unentitled")

        self.assertEqual(email, AUTHORISED_EMAIL)
        self.assertFalse(is_authorised)

    def test_no_email_token_raises_the_actionable_403(self):
        # Null mail, not a missing key: the user can fix this themselves, so
        # it must be NoUserEmailError (403) and never a contract error (503).
        with use_provider(AUTH_PROVIDER_FAKE):
            with self.assertRaises(NoUserEmailError):
                validate_token(FAKE_TOKEN_NO_EMAIL)

    def test_an_unknown_token_is_not_a_free_pass(self):
        # The trap this fake exists to avoid reproducing wrongly: the real
        # endpoint answers 200 + loggedIn:false for absent, garbage and
        # forged tokens, so an unknown token must be unauthenticated here
        # too -- never an accidental login.
        for value in ("", "   ", "garbage", "eyJhbGciOiJIUzI1NiJ9.e30.x"):
            with self.subTest(token=value):
                with use_provider(AUTH_PROVIDER_FAKE):
                    with self.assertRaises(NotAuthenticatedError):
                        validate_token(value)

    def test_header_hygiene_still_applies_to_dev_tokens(self):
        # _assert_sendable runs before the fake lookup, so the fake cannot
        # be used to smuggle something unsendable past it.
        with use_provider(AUTH_PROVIDER_FAKE):
            with self.assertRaises(NotAuthenticatedError):
                validate_token("dev-authorised\r\nX-Admin: true")

    def test_the_fixtures_are_the_ones_the_auth_tests_use(self):
        # If these diverge, the fake stops representing the real endpoint
        # and this whole approach is worthless.
        for name in ("anonymous.json", "authorised.json", "unentitled.json"):
            with self.subTest(fixture=name):
                path = cloudgene_auth.FIXTURES_PATH / name
                self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
