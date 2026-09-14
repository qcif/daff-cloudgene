"""Validate a Cloudgene ``X-Auth-Token`` by asking Cloudgene about it.

The uploader has no login of its own. It forwards the client's opaque token
to Cloudgene's ``/api/v2/server`` endpoint and treats the answer as
authoritative, per §5.2 of ``uploader/spec/client-azure-upload.md``.

Two rules govern this module:

1. **The JWT is never parsed for identity.** Its payload is readable without
   the signing key, so it is untrusted input. The only thing taken from the
   client is the opaque token string; the only thing forwarded to Cloudgene is
   that same string, unmodified.
2. **Every failure path fails closed.** Cloudgene unreachable, a timeout, a
   non-200 status, a body that is not JSON, a missing ``loggedIn``, a missing
   ``user.mail`` — all raise. Nothing returns a permissive default.

Observed behaviour of the endpoint (recorded 2026-09-14 against the public
host, anonymous caller):

- No token, a garbage token, and a well-formed JWT with an invalid signature
  all return **HTTP 200** with ``loggedIn: false``, ``apps: []`` and **no**
  ``user`` key at all. A 200 is therefore never evidence of authentication.

Base URL
--------
``CLOUDGENE_BASE_URL`` selects the host. The default is the public HTTPS host
because loopback reachability (§8.1 of the task brief) has not yet been
confirmed by the operator. Once it is, set
``CLOUDGENE_BASE_URL=http://127.0.0.1:8082`` in the systemd unit to drop TLS
and DNS from the request path.
"""

import os
import re

import requests

CLOUDGENE_PUBLIC_BASE_URL = "https://cloudgene.qcif.edu.au"
CLOUDGENE_LOOPBACK_BASE_URL = "http://127.0.0.1:8082"
CLOUDGENE_BASE_URL_ENV_VAR = "CLOUDGENE_BASE_URL"
SERVER_ENDPOINT_PATH = "/api/v2/server"
AUTH_TOKEN_HEADER = "X-Auth-Token"
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 8192
RX_ILLEGAL_HEADER_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class CloudgeneAuthError(Exception):
    """Base class for every failure in this module.

    A caller that catches only this class still fails closed, because no code
    path returns a value on error.
    """


class CloudgeneUnavailableError(CloudgeneAuthError):
    """Cloudgene could not be reached, or did not answer usefully.

    Maps to HTTP 503 in the uploader's API. Includes timeouts, connection
    errors, non-200 statuses and bodies that are not JSON.
    """


class CloudgeneContractError(CloudgeneAuthError):
    """Cloudgene answered, but not in the shape this module understands.

    Maps to HTTP 503. Raised when ``loggedIn`` is absent or not a boolean,
    when ``apps`` is absent or not a list, or when a logged-in response
    carries no usable ``user.mail``. This is the signal that a Cloudgene
    upgrade changed the endpoint — the correct response is to fail loudly,
    never to degrade to allow-all.
    """


class NotAuthenticatedError(CloudgeneAuthError):
    """The token is absent, malformed, expired, forged or logged out.

    Maps to HTTP 401. The client should redirect to the Cloudgene login.
    """


class NoUserEmailError(CloudgeneAuthError):
    """The user is authenticated, but their account carries no email address.

    Cloudgene permits accounts without an email — its own UI offers to "enter
    your email address at any time to upgrade your account". Such a user is
    legitimately logged in and may well be entitled to a workflow, but the
    uploader cannot place their blobs, because the email *is* the storage
    prefix (§7 of the spec).

    Maps to HTTP 403, with a message telling the user to set an email address
    in Cloudgene. Deliberately distinct from :class:`CloudgeneContractError`:
    this is an expected user state, not a broken endpoint, and must not look
    like an outage.
    """


def get_base_url() -> str:
    """Return the Cloudgene base URL, from the environment or the default."""
    return os.environ.get(
        CLOUDGENE_BASE_URL_ENV_VAR,
        CLOUDGENE_PUBLIC_BASE_URL,
    ).rstrip("/")


def _assert_sendable(auth_token: str) -> str:
    """Reject tokens that must not be placed in a request header.

    An empty or non-string token is a logged-out client, not an error worth
    a round trip. Control characters would permit header injection, so they
    are refused outright rather than forwarded.
    """
    if not isinstance(auth_token, str):
        raise NotAuthenticatedError("No auth token supplied")

    token = auth_token.strip()
    if not token:
        raise NotAuthenticatedError("No auth token supplied")

    if len(token) > MAX_TOKEN_LENGTH:
        raise NotAuthenticatedError("Auth token is implausibly long")

    if RX_ILLEGAL_HEADER_CHARS.search(token):
        raise NotAuthenticatedError("Auth token contains illegal characters")

    return token


def fetch_server_info(
    auth_token: str,
    base_url: str = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """Forward the token to Cloudgene and return the decoded JSON body.

    Raises:
        NotAuthenticatedError: the token could not be sent at all.
        CloudgeneUnavailableError: Cloudgene was unreachable, timed out,
            returned a non-200 status, or returned a body that is not a JSON
            object.
    """
    token = _assert_sendable(auth_token)
    url = (base_url.rstrip("/") if base_url else get_base_url()) \
        + SERVER_ENDPOINT_PATH

    try:
        response = requests.get(
            url,
            headers={
                AUTH_TOKEN_HEADER: token,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CloudgeneUnavailableError(
            f"Could not reach Cloudgene at {url}: {exc.__class__.__name__}"
        ) from exc

    if response.status_code != 200:
        raise CloudgeneUnavailableError(
            f"Cloudgene returned HTTP {response.status_code} from {url}")

    try:
        data = response.json()
    except ValueError as exc:
        raise CloudgeneUnavailableError(
            f"Cloudgene returned a non-JSON body from {url}") from exc

    if not isinstance(data, dict):
        raise CloudgeneUnavailableError(
            f"Cloudgene returned a JSON {type(data).__name__}, expected an "
            "object")

    return data


def interpret_server_info(data: dict) -> tuple[str, bool]:
    """Apply the authorisation rule to a decoded ``/api/v2/server`` body.

    Separated from the HTTP call so that the decision logic can be tested
    against recorded fixtures without a network.

    Returns:
        ``(email, is_authorised)`` where ``email`` is the lowercased
        ``user.mail`` and ``is_authorised`` is ``len(apps) > 0``.
        ``deprecatedApps`` and ``experimentalApps`` are deliberately ignored.

    Raises:
        NotAuthenticatedError: ``loggedIn`` is false.
        CloudgeneContractError: the response shape is not what §5.2 expects.
    """
    if "loggedIn" not in data:
        raise CloudgeneContractError(
            "Cloudgene response has no 'loggedIn' field")

    logged_in = data["loggedIn"]
    if not isinstance(logged_in, bool):
        raise CloudgeneContractError(
            "Cloudgene response field 'loggedIn' is "
            f"{type(logged_in).__name__}, expected bool")

    if not logged_in:
        raise NotAuthenticatedError(
            "Cloudgene reports the token is not logged in")

    # The response also carries 'deprecatedApps' and 'experimentalApps'.
    # Neither counts toward authorisation (decided 2026-09-14): entitlement to
    # a deprecated or experimental app is not permission to upload. A user
    # whose only entitlement sits in those lists is denied, deliberately.
    apps = data.get("apps")
    if not isinstance(apps, list):
        raise CloudgeneContractError(
            "Cloudgene response field 'apps' is "
            f"{type(apps).__name__}, expected list")

    user = data.get("user")
    if not isinstance(user, dict):
        raise CloudgeneContractError(
            "Cloudgene reported loggedIn=true but returned no 'user' object")

    if "mail" not in user:
        raise CloudgeneContractError(
            "Cloudgene reported loggedIn=true but the 'user' object has no "
            "'mail' key at all")

    mail = user["mail"]
    if mail is None or (isinstance(mail, str) and not mail.strip()):
        raise NoUserEmailError(
            "This Cloudgene account has no email address set. Add one in "
            "Cloudgene, then retry.")

    if not isinstance(mail, str):
        raise CloudgeneContractError(
            f"Cloudgene field 'user.mail' is {type(mail).__name__}, "
            "expected str")

    return mail.strip().lower(), len(apps) > 0


def validate_token(auth_token: str) -> tuple[str, bool]:
    """Return ``(email, is_authorised)`` for a Cloudgene X-Auth-Token.

    Raises on any failure to reach or interpret Cloudgene — callers must
    fail closed, never default-allow.

    ``is_authorised`` is ``False`` only for the one non-error denial: a
    genuinely logged-in user who is entitled to no workflow. Callers should
    map that to HTTP 403, :class:`NotAuthenticatedError` to 401,
    :class:`NoUserEmailError` to 403 (surfacing its message, which tells the
    user what to fix), and both :class:`CloudgeneUnavailableError` and
    :class:`CloudgeneContractError` to 503.
    """
    return interpret_server_info(fetch_server_info(auth_token))


def startup_self_test(base_url: str = None) -> None:
    """Assert the endpoint still has the shape this module depends on.

    Called at application startup, per §5.3 of the spec. It uses an
    unauthenticated request, so it needs no credential: the anonymous
    response must still be an HTTP 200 JSON object carrying ``loggedIn:
    false`` and an empty ``apps`` list. Raises rather than returning a
    verdict, so a failed self-test stops the service instead of degrading it.
    """
    url = (base_url.rstrip("/") if base_url else get_base_url()) \
        + SERVER_ENDPOINT_PATH

    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CloudgeneUnavailableError(
            f"Startup self-test could not read {url}: "
            f"{exc.__class__.__name__}") from exc

    if response.status_code != 200:
        raise CloudgeneUnavailableError(
            f"Startup self-test: {url} returned HTTP {response.status_code}")

    if data.get("loggedIn") is not False:
        raise CloudgeneContractError(
            "Startup self-test: anonymous response did not carry "
            "loggedIn=false; the endpoint contract has changed")

    if data.get("apps") != []:
        raise CloudgeneContractError(
            "Startup self-test: anonymous response did not carry an empty "
            "'apps' list; the endpoint contract has changed")
