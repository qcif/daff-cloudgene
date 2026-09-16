"""FastAPI surface for the Azure uploader.

Implements §6, §7, §10 and §11 of
``uploader/spec/client-azure-upload.md``. The file bytes never pass through
here: this service validates a Cloudgene session, reserves a blob path,
hands back a short-lived create-only SAS, and later reconciles what actually
landed in the container against what was promised.

Shape
-----
Issuance, completion, renewal, status, listing and deletion. Every one of
them resolves identity through :func:`cloudgene_auth.validate_token` via the
:func:`require_user` dependency, so a new route cannot quietly ship
unauthenticated. ``/healthz`` is the exception, deliberately: it answers for
*this process*, and must not report unhealthy because Cloudgene is
restarting.

``root_path="/uploads/api"`` matches the trailing slash on nginx's
``proxy_pass`` (§6 of the spec), which strips the prefix before FastAPI sees
the request.

Trust boundaries
----------------
- Identity is ``user.mail`` from Cloudgene, never the request body. A client
  that posts an ``email`` field is ignored (§12 of the spec).
- ``GET /uploads/{id}`` is scoped to the resolved email, not just the ID. An
  upload ID that leaks must not become an authorisation bypass.
- ``DELETE /files`` takes a path *within* the caller's prefix and rebuilds
  the blob path from the resolved identity, so naming another user's blob
  is unexpressible rather than merely refused.
- The completion callback is a **trigger**, never evidence. The verdict
  comes from blob properties read server-side (§4.5 of the task brief).
- Nothing that could be a credential — the token, the client secret, a SAS
  query string, a traceback — reaches a response body or a log line.
"""

import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import azure_sas
import naming
import storage
from cloudgene_auth import (
    AUTH_PROVIDER_FAKE,
    AUTH_TOKEN_HEADER,
    CloudgeneContractError,
    CloudgeneUnavailableError,
    NoUserEmailError,
    NotAuthenticatedError,
    get_auth_provider,
    startup_self_test,
    validate_token,
)
from config import ISSUER_AZURE, ConfigError, load_config, redact
from storage import RenewalRefusedError, UploadNotFoundError, UploadStore

logger = logging.getLogger("uploader")

ROOT_PATH = "/uploads/api"
API_TITLE = "DAFF Biosecurity uploader"
API_VERSION = "1.0.0"

MAX_FILENAME_LENGTH = 1024
MAX_CONTENT_TYPE_LENGTH = 255

GENERIC_UNAUTHENTICATED = (
    "Not signed in to Cloudgene. Sign in and try again.")
GENERIC_UNAVAILABLE = (
    "The upload service cannot verify your session right now. Try again "
    "shortly.")
NOT_ENTITLED_MESSAGE = (
    "Your Cloudgene account does not have access to any workflow, so no "
    "upload location can be issued. Ask an administrator for access.")


class NotEntitledError(Exception):
    """A logged-in user entitled to no workflow.

    The one non-error denial in the authorisation rule (§5.2 of the spec):
    ``loggedIn`` is true, ``apps`` is empty. Maps to HTTP 403.
    """


class ValidationFailedError(Exception):
    """A declared size, content type or path failed a check.

    Maps to HTTP 400, naming the check that failed so the client can fix the
    request rather than guess.
    """


class UploadInProgressError(Exception):
    """A delete was asked for a blob an unexpired SAS still points at.

    Maps to HTTP 409. Deleting does not revoke the outstanding capability,
    so an upload mid-transfer would commit its blocks after the delete and
    re-create the blob — leaving the user staring at a file they deleted.
    """


class RateLimitExceededError(Exception):
    """This user has been issued too many SAS tokens too recently.

    Maps to HTTP 429 with ``Retry-After``. Blast-radius control per §10 of
    the spec: a compromised account must not be able to mint unlimited
    write capabilities.
    """

    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class UploadRequest(BaseModel):
    """The client's declaration for one file.

    Extra fields are ignored, which is the point: a client that supplies an
    ``email``, ``username`` or ``role`` gets no effect from it (§12 of the
    spec). Size and content type are advisory — §10 explains why a SAS
    cannot enforce either — and are reconciled after the fact.
    """

    filename: str = Field(..., max_length=MAX_FILENAME_LENGTH)
    size: int = Field(..., ge=1)
    content_type: str = Field(..., max_length=MAX_CONTENT_TYPE_LENGTH)


class DeleteRequest(BaseModel):
    """The client's nomination of one file to delete, within its own prefix.

    ``client_path`` is the portion *after* the user's prefix, never a blob
    path: the server rebuilds the full path from the resolved identity with
    the same :func:`naming.build_blob_path` that ``POST /uploads`` uses. A
    caller therefore cannot name another user's blob — not "is refused",
    but has no way to express it.

    A JSON body rather than a path parameter because blob paths contain
    ``/`` and an email address, so a path parameter would mean
    double-encoding through nginx and a ``{path:path}`` route — a shape that
    invites a traversal bug for no benefit.
    """

    client_path: str = Field(..., max_length=MAX_FILENAME_LENGTH)


def get_config(request: Request):
    """Return the process configuration resolved at startup."""
    return request.app.state.config


def get_store(request: Request) -> UploadStore:
    """Return the pending-upload store."""
    return request.app.state.store


def get_issuer(request: Request):
    """Return the configured SAS issuer."""
    return request.app.state.issuer


def require_user(
    x_auth_token: str = Header(None, alias=AUTH_TOKEN_HEADER),
) -> str:
    """Resolve the caller's email, or raise.

    A dependency rather than a helper so that it cannot be forgotten on a
    new route. Every outcome is logged per §11 — the resolved email and the
    reason for any denial — and the token never is.

    Raises:
        NotAuthenticatedError: 401.
        NoUserEmailError: 403, message surfaced.
        NotEntitledError: 403.
        CloudgeneUnavailableError, CloudgeneContractError: 503.
    """
    email, is_authorised = validate_token(x_auth_token)

    if not is_authorised:
        logger.info(
            "auth denied user=%s reason=no-workflow-entitlement", email)
        raise NotEntitledError(NOT_ENTITLED_MESSAGE)

    logger.info("auth allowed user=%s", email)
    return email


def check_declaration(config, request_body: UploadRequest) -> None:
    """Reject the cheaply detectable mistakes before signing anything.

    Advisory only. A SAS constrains which blob, which operation and for how
    long; it constrains nothing about the bytes (§10 of the spec), so these
    checks exist to catch honest errors, not to be a security control. The
    real check is reconciliation.
    """
    if request_body.size > config.max_upload_bytes:
        raise ValidationFailedError(
            f"Declared size {request_body.size} exceeds the maximum of "
            f"{config.max_upload_bytes} bytes")

    content_type = request_body.content_type.split(";")[0].strip().lower()
    if content_type not in config.allowed_content_types:
        raise ValidationFailedError(
            f"Content type {content_type!r} is not in the accepted list")

    _, _, extension = request_body.filename.rpartition(".")
    extension = f".{extension.lower()}" if extension else ""
    if extension not in config.allowed_extensions:
        raise ValidationFailedError(
            f"File extension {extension or '(none)'} is not in the accepted "
            "list")


def check_rate_limit(config, store: UploadStore, email: str) -> None:
    """Raise if this user is over their issuance budget for the window.

    Keyed on the resolved email, not the IP: the budget belongs to the
    account, and a single account behind a corporate NAT must not be able to
    spend everyone else's.
    """
    since = storage.window_start(config.rate_limit_window_seconds)
    issued = store.count_issued_since(email, since)

    if issued < config.rate_limit_max:
        return

    oldest = store.oldest_issued_since(email, since)
    retry_after = config.rate_limit_window_seconds
    if oldest is not None:
        elapsed = (storage.utcnow() - oldest).total_seconds()
        retry_after = max(
            1,
            math.ceil(config.rate_limit_window_seconds - elapsed),
        )

    logger.warning(
        "rate limit tripped user=%s issued=%s window=%ss",
        email, issued, config.rate_limit_window_seconds)

    raise RateLimitExceededError(
        f"Too many upload tokens issued: {issued} in the last "
        f"{config.rate_limit_window_seconds} seconds.",
        retry_after_seconds=retry_after,
    )


def reconcile_upload(
    upload: storage.Upload,
    store: UploadStore,
    reader,
) -> tuple:
    """Compare the real blob against the record and resolve the record.

    Deliberately takes a record and a reader rather than a request, so that
    an Event Grid webhook could call it unchanged if §6 of
    ``2_azure_resources.md`` is ever actioned.

    Idempotent: the transition is a conditional ``UPDATE ... WHERE state =
    'pending'``, so a second delivery changes nothing and reports the
    outcome the first one reached (§12 of the spec).

    Returns:
        ``(record, changed)`` — the record as it now stands, and whether
        this call is the one that resolved it.
    """
    properties = reader.get_blob_properties(upload.blob_path)

    if properties is None:
        state = storage.STATE_FAILED
        detail = "No blob exists at the reserved path"
    elif properties.size != upload.declared_size:
        state = storage.STATE_FAILED
        detail = (
            f"Blob is {properties.size} bytes; {upload.declared_size} were "
            "declared")
    elif not _content_type_matches(properties.content_type, upload):
        state = storage.STATE_FAILED
        detail = (
            f"Blob content type {properties.content_type!r} does not match "
            f"the declared {upload.declared_content_type!r}")
    else:
        state = storage.STATE_COMPLETED
        detail = None

    changed = store.resolve(
        upload.upload_id,
        state,
        detail=detail,
        user_email=upload.user_email,
    )

    record = store.get(upload.upload_id, upload.user_email)

    logger.info(
        "reconciled upload=%s user=%s blob=%s state=%s changed=%s detail=%s",
        record.upload_id, record.user_email, record.blob_path, record.state,
        changed, record.detail)

    return record, changed


def _content_type_matches(actual: str, upload: storage.Upload) -> bool:
    """Return whether the blob's content type agrees with the declaration.

    A blob with no content type set at all passes. The client controls the
    header either way (§10), so treating an absent value as a failure would
    reject correct uploads from clients that simply did not set it, while
    catching no attack that setting the right value would not evade.
    """
    if not actual:
        return True

    declared = upload.declared_content_type.split(";")[0].strip().lower()
    return actual.split(";")[0].strip().lower() == declared


def serialise(upload: storage.Upload, container: str) -> dict:
    """Return the client-facing view of an upload record.

    No SAS and no token: this is returned on status reads, which may happen
    long after issuance. ``container`` is passed in rather than read from a
    module-level global, since this function has no config of its own.
    """
    return {
        "upload_id": upload.upload_id,
        "blob_path": upload.blob_path,
        "az_path": f"az://{container}/{upload.blob_path}",
        "declared_size": upload.declared_size,
        "declared_content_type": upload.declared_content_type,
        "issued_at": upload.issued_at.isoformat(),
        "expires_at": upload.expires_at.isoformat(),
        "state": upload.state,
        "detail": upload.detail,
    }


def _error(status_code: int, message: str, headers: dict = None):
    """Return a uniform error body.

    One shape for every failure, and never a traceback, a token, a secret or
    a SAS query string.
    """
    return JSONResponse(
        status_code=status_code,
        content={"detail": message},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the §4.7 taxonomy, once, for every route.

    Centralised so that no route invents its own mapping and no future route
    forgets one.
    """

    @app.exception_handler(NotAuthenticatedError)
    async def _not_authenticated(request: Request, exc):
        logger.info("auth denied reason=not-authenticated")
        return _error(401, GENERIC_UNAUTHENTICATED)

    @app.exception_handler(NoUserEmailError)
    async def _no_user_email(request: Request, exc):
        # Surfaced verbatim: the message tells the user exactly what to fix,
        # and the state is theirs to fix. A generic 403 here would send
        # someone to the helpdesk for a two-click change.
        logger.info("auth denied reason=no-user-email")
        return _error(403, str(exc))

    @app.exception_handler(NotEntitledError)
    async def _not_entitled(request: Request, exc):
        return _error(403, str(exc))

    @app.exception_handler(CloudgeneContractError)
    async def _contract(request: Request, exc):
        # Loud: this means a Cloudgene upgrade changed the endpoint this
        # service depends on, and the correct response is to be noticed.
        logger.error(
            "CLOUDGENE CONTRACT VIOLATION — /api/v2/server no longer has the "
            "expected shape: %s", exc)
        return _error(503, GENERIC_UNAVAILABLE)

    @app.exception_handler(CloudgeneUnavailableError)
    async def _unavailable(request: Request, exc):
        logger.warning("Cloudgene unavailable: %s", exc)
        return _error(503, GENERIC_UNAVAILABLE)

    @app.exception_handler(azure_sas.SasError)
    async def _sas_error(request: Request, exc):
        logger.error("SAS issuance failed: %s", exc)
        return _error(
            503,
            "The upload service could not reach storage. Try again shortly.")

    @app.exception_handler(ValidationFailedError)
    async def _validation_failed(request: Request, exc):
        return _error(400, str(exc))

    @app.exception_handler(naming.InvalidPathError)
    async def _invalid_path(request: Request, exc):
        return _error(400, f"Invalid file path: {exc}")

    @app.exception_handler(naming.InvalidEmailError)
    async def _invalid_email(request: Request, exc):
        # The email came from Cloudgene, so this is our problem, not the
        # caller's — do not echo it back.
        logger.error("Cloudgene returned an unusable email: %s", exc)
        return _error(503, GENERIC_UNAVAILABLE)

    @app.exception_handler(UploadInProgressError)
    async def _upload_in_progress(request: Request, exc):
        return _error(409, str(exc))

    @app.exception_handler(RateLimitExceededError)
    async def _rate_limited(request: Request, exc):
        return _error(
            429,
            str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(UploadNotFoundError)
    async def _not_found(request: Request, exc):
        return _error(404, "No such upload")

    @app.exception_handler(RenewalRefusedError)
    async def _renewal_refused(request: Request, exc):
        if exc.upload.state != storage.STATE_PENDING:
            return _error(
                409,
                "This upload is no longer pending and cannot be renewed. "
                "Start a new upload instead.")
        return _error(
            429,
            "This upload has reached its renewal limit.")

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc):
        # FastAPI's default is 422; §4.7 of the brief puts every validation
        # failure at 400, so the client has one case to handle.
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"][1:])
            for error in exc.errors()
        )
        return _error(400, f"Invalid request body: {fields or 'malformed'}")


def register_routes(app: FastAPI) -> None:
    """Install the routes of §4.1, plus listing and renewal."""

    @app.get("/healthz")
    def healthz():
        """Liveness for this process only.

        No authentication and no call to Cloudgene, on purpose: a health
        check that fails when a dependency is restarting tells the
        supervisor to kill a process that is working fine.
        """
        return {"status": "ok"}

    @app.post("/uploads", status_code=201)
    def create_upload(
        body: UploadRequest,
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
        issuer=Depends(get_issuer),
    ):
        """Validate, reserve a blob path, and issue a create-only SAS."""
        check_declaration(config, body)
        check_rate_limit(config, store, email)

        blob_path = naming.build_blob_path(email, body.filename)

        # Opportunistic sweep. There is no scheduler on this host, and the
        # cost is one indexed UPDATE, so lapsed records are closed out on
        # the next issuance rather than accumulating as 'pending' forever.
        store.expire_pending()

        expires_at = storage.utcnow() + timedelta(
            seconds=config.sas_ttl_seconds)
        issued = issuer.issue(blob_path, expires_at)

        record = store.create(
            user_email=email,
            blob_path=blob_path,
            declared_size=body.size,
            declared_content_type=body.content_type,
            expires_at=issued.expires_at,
        )

        # The SAS query string is never logged — it is the capability.
        logger.info(
            "issued upload=%s user=%s blob=%s size=%s expires=%s",
            record.upload_id, email, blob_path, body.size,
            record.expires_at.isoformat())

        return {
            **serialise(record, config.storage_container),
            "upload_url": issued.url,
        }

    @app.post("/uploads/{upload_id}/complete")
    def complete_upload(
        upload_id: str,
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
        issuer=Depends(get_issuer),
    ):
        """Reconcile the real blob against the record.

        The request body, if any, is ignored entirely. The client's report
        is a trigger; the verdict comes from the blob's real properties,
        read server-side.

        Calling this twice is safe and returns the same verdict both times.
        """
        upload = store.get(upload_id, email)

        if upload.is_terminal:
            # Already resolved — report the outcome rather than re-running
            # it or erroring. §12 of the spec: one delivery or two, one
            # outcome.
            return {
                **serialise(upload, config.storage_container),
                "changed": False,
            }

        record, changed = reconcile_upload(upload, store, issuer)
        return {
            **serialise(record, config.storage_container),
            "changed": changed,
        }

    @app.get("/uploads/{upload_id}")
    def get_upload(
        upload_id: str,
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
    ):
        """Return the status of one upload, scoped to its owner."""
        upload = store.get(upload_id, email)
        return serialise(upload, config.storage_container)

    @app.post("/uploads/{upload_id}/renew")
    def renew_upload(
        upload_id: str,
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
        issuer=Depends(get_issuer),
    ):
        """Re-sign the same blob path and extend the record's expiry.

        Resumability within a session — surviving a network interruption or
        a SAS that expires mid-transfer — needs a way to re-sign the same
        blob path, since ``POST /uploads`` always mints a new one. Owner
        scoping comes from ``store.get`` and is repeated inside
        ``store.renew``, so a leaked upload ID is not a bypass and a miss
        is a 404 covering both "no such upload" and "not yours".
        """
        # Owner-scoped lookup first, so a miss is a clean 404 before any
        # SAS is signed.
        current = store.get(upload_id, email)

        expires_at = storage.utcnow() + timedelta(
            seconds=config.sas_ttl_seconds)
        issued = issuer.issue(current.blob_path, expires_at)

        # The conditional UPDATE inside store.renew() is the real gate —
        # pending state and the renewal cap, checked atomically. The
        # lookup above exists only to sign against the right blob path.
        record = store.renew(
            upload_id, email, issued.expires_at, config.max_renewals)

        logger.info(
            "renewed upload=%s user=%s blob=%s renewal_count=%s expires=%s",
            record.upload_id, email, record.blob_path, record.renewal_count,
            record.expires_at.isoformat())

        return {
            **serialise(record, config.storage_container),
            "upload_url": issued.url,
        }

    @app.get("/files")
    def list_files(
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
        issuer=Depends(get_issuer),
    ):
        """List the caller's own blobs, annotated with matching records.

        Azure is the source of truth, not the SQLite table: the two diverge
        whenever a blob is deleted out of band, or a client uploads its
        bytes and never calls back. Nothing from the request can influence
        the prefix — it is always ``naming.prefix_for(email)``, built from
        the resolved identity, never from a query parameter.
        """
        prefix = naming.prefix_for(email)
        blobs = issuer.list_blobs(prefix)

        truncated = len(blobs) > config.max_list_results
        blobs = blobs[:config.max_list_results]

        # Newest first, so the first record seen for a given blob path is
        # the one kept when more than one exists (e.g. two tokens issued
        # for the same logical file).
        records_by_blob_path = {}
        for record in store.list_for_owner(
                email, limit=config.max_list_results):
            records_by_blob_path.setdefault(record.blob_path, record)

        files = []
        for blob in blobs:
            entry = {
                "blob_path": blob.blob_path,
                # What DELETE /files takes back. Sent from here rather than
                # left for the client to derive by slicing off a prefix it
                # would have to reconstruct: the two values must come from
                # the same source, and this is that source.
                "client_path": blob.blob_path[len(prefix):],
                "az_path": config.az_path(blob.blob_path),
                "size": blob.size,
                "content_type": blob.content_type,
                "last_modified": (
                    blob.last_modified.isoformat()
                    if blob.last_modified else None),
            }
            record = records_by_blob_path.get(blob.blob_path)
            if record is not None:
                entry["upload_id"] = record.upload_id
                entry["state"] = record.state
            files.append(entry)

        return {"files": files, "truncated": truncated}

    @app.delete("/files")
    def delete_file(
        body: DeleteRequest,
        email: str = Depends(require_user),
        config=Depends(get_config),
        store: UploadStore = Depends(get_store),
        issuer=Depends(get_issuer),
    ):
        """Delete one blob from the caller's own prefix.

        Server-side, under the service principal: every SAS issued here is
        create-only, and widening it so the browser could delete would cost
        the strongest property this design has.

        Idempotent. A blob that is already gone is ``200`` with
        ``deleted: false``, not ``404`` — a double-click, a retry after a
        flaky response and two tabs racing all converge on one outcome, and
        the UI does the same thing either way.

        The record table is not touched. Records are the issuance history —
        what was promised and how it resolved — while the container is the
        truth about what exists, so a deleted blob simply stops appearing in
        ``GET /files``.
        """
        blob_path = naming.build_blob_path(email, body.client_path)
        prefix = naming.prefix_for(email)

        # build_blob_path already guarantees this. The assertion is here so
        # that a future refactor of naming.py fails a test rather than
        # leaking a delete outside the caller's prefix.
        if not blob_path.startswith(prefix):
            logger.error(
                "refusing to delete a path outside the caller's prefix "
                "user=%s blob=%s", email, blob_path)
            raise naming.InvalidPathError(
                "Path escaped the user's prefix")

        in_progress = store.find_live_pending(email, blob_path)
        if in_progress is not None:
            raise UploadInProgressError(
                "An upload to this path is in progress; cancel it or wait "
                "for it to finish, then delete.")

        existed = issuer.delete_blob(blob_path)

        logger.info(
            "deleted blob user=%s blob=%s existed=%s",
            email, blob_path, str(existed).lower())

        return {
            "deleted": existed,
            "blob_path": blob_path,
            "az_path": config.az_path(blob_path),
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and the Cloudgene contract before serving.

    Both checks raise. A service that starts misconfigured and 500s on
    demand is worse than one that refuses to start (§4.8 of the brief), and
    a ``/api/v2/server`` whose shape has changed must fail loudly rather
    than degrade toward allow-all (§5.3 of the spec).
    """
    config = load_config()
    auth_provider = get_auth_provider()
    logger.info(
        "Starting uploader with auth_provider=%s config %s",
        auth_provider, redact(config))

    # The one combination that must never run: an auth provider that hands
    # out identities without asking Cloudgene, wired to an issuer that mints
    # real, usable write capabilities against the production container. Each
    # half is safe alone — fake auth is for local development, the Azure
    # issuer is production — and together they are an open door.
    if auth_provider == AUTH_PROVIDER_FAKE and config.issuer == ISSUER_AZURE:
        raise ConfigError(
            "UPLOADER_AUTH_PROVIDER=fake cannot be combined with "
            "UPLOADER_SAS_ISSUER=azure: that would issue real Azure write "
            "capabilities to unauthenticated callers. Use the fake SAS "
            "issuer for local development.")

    startup_self_test()

    store = UploadStore(config.database_path)
    store.initialise()

    app.state.config = config
    app.state.store = store
    app.state.issuer = azure_sas.create_issuer(config)

    yield


def create_app() -> FastAPI:
    """Build the application.

    A factory so tests can build an isolated instance; ``app`` below is what
    uvicorn serves.
    """
    application = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        root_path=ROOT_PATH,
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    register_routes(application)
    return application


logging.basicConfig(
    level=os.environ.get("UPLOADER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()

__all__ = [
    "app",
    "create_app",
    "reconcile_upload",
    "ConfigError",
]
