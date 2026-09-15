"""The two PUT routes, CORS, and Azure-shaped errors.

Stands in for Azure Blob Storage's write surface during local development
(``uploader/spec/tasks/06-mock-azure.md``). Runs as its own ASGI process on
its own port — never mounted inside ``uploader/api/app.py`` — so that it is
structurally impossible to ship: a module the production app never imports
cannot become reachable in production by a stray refactor.

The wire surface is exactly two requests, measured against the SDK the
client bundles (§0 of the task brief):

    PUT /{container}/{blob}?{sas}&comp=block&blockid=...
        body: the raw block bytes

    PUT /{container}/{blob}?{sas}&comp=blocklist
        body: <BlockList><Latest>...</Latest>...</BlockList>

No ``GET``, no ``HEAD``, no ``Authorization`` header, and ``sig`` is never
checked — there is nothing to verify and nothing gained by pretending.
Anything else — an unrecognised ``comp=``, a lapsed ``se=``, a path that
does not survive ``naming``'s traversal defences, a commit onto an existing
blob — is refused loudly with an Azure-shaped error, never a cheerful 201.
The real risk of a hand-rolled stand-in is a client bug that passes here
and fails against production; loud rejection is what keeps the two in step.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from store import BlobAlreadyExistsError, DevBlobStore, UnknownBlockError

# Reuse the traversal defences in uploader/api/naming.py rather than
# writing new ones (§3 of the task brief) — this is a dev tool, but one
# that writes attacker-controlled paths to disk. uploader/api/** must never
# import this package (asserted by a test in uploader/api/tests/); the
# reverse — this standalone process borrowing api's path validation — is
# not the forbidden direction, since this module never runs in production.
_API_DIR = Path(__file__).resolve().parent.parent / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

import naming  # noqa: E402

logger = logging.getLogger("devblob")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8004
ENV_ROOT = "UPLOADER_LOCAL_BLOB_ROOT"
DEFAULT_ROOT = "/tmp/uploader-blobs"

ENV_DEV_ORIGIN = "UPLOADER_DEV_ORIGIN"
DEFAULT_DEV_ORIGIN = "http://localhost:5173"

COMP_BLOCK = "block"
COMP_BLOCKLIST = "blocklist"

# The measured requests in §0 of the task brief carry x-ms-version and
# x-ms-client-request-id on every call, which is not in §8's allowed-headers
# list for the storage account CORS rule — see the task's completion notes
# for the escalation. A live run against a real browser (§9 verification)
# turned up a second, undocumented one: the bundled SDK also sends
# x-ms-useragent (client telemetry — SDK, core-rest-pipeline and browser
# versions), on every request, unconditionally. §0 did not catch it because
# it was measured against the SDK's network calls in isolation, not a real
# preflight in a real browser. The local stand-in accepts the full set so
# the local flow works regardless of how these discrepancies are resolved
# in production.
CORS_ALLOWED_HEADERS = [
    "x-ms-blob-type",
    "x-ms-blob-content-type",
    "content-type",
    "content-length",
    "x-ms-version",
    "x-ms-client-request-id",
    "x-ms-useragent",
]
CORS_EXPOSED_HEADERS = ["etag", "x-ms-request-id"]
CORS_MAX_AGE = 3600

SAS_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

XML_CONTENT_TYPE = "application/xml"
FAKE_ETAG = '"devblob"'
FAKE_REQUEST_ID = "00000000-0000-0000-0000-000000000000"


def azure_error(status_code: int, code: str, message: str) -> Response:
    """Return an Azure-shaped error: ``x-ms-error-code`` plus an XML body.

    The SDK surfaces the code as ``RestError.code``, so this is what makes
    the client's error handling exercise its real branches rather than a
    generic failure path.
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type=XML_CONTENT_TYPE,
        headers={"x-ms-error-code": code},
    )


def parse_block_list(xml_body: bytes) -> list:
    """Return the block IDs from a ``<BlockList>`` document, in order.

    Order comes from this list, not from arrival order or a filename sort
    (§3 of the task brief) — the client stages blocks with six-way
    concurrency, so they land on disk out of order routinely.
    """
    root = ElementTree.fromstring(xml_body)
    return [
        (element.text or "").strip()
        for element in root
        if _local_tag(element.tag) in {"Latest", "Committed", "Uncommitted"}
    ]


def _local_tag(tag: str) -> str:
    """Strip any XML namespace off an element tag."""
    return tag.rsplit("}", 1)[-1]


def _validate_blob_path(container: str, blob_path: str) -> str:
    """Return the validated blob path, or raise.

    Raises :class:`naming.InvalidPathError` or
    :class:`naming.InvalidEmailError`. The URL shape is
    ``/{container}/{email}/{client path}`` — the same ``<email>/<client
    path>`` blob naming the API constructs, so this reuses
    :func:`naming.build_blob_path` rather than inventing a parallel check.
    """
    email, separator, leaf = blob_path.partition("/")
    if not separator:
        raise naming.InvalidPathError("Path has no leaf component")
    return naming.build_blob_path(email, leaf)


def _is_expired(query, now: datetime = None) -> bool:
    """Return whether ``se=`` in the query string has passed.

    ``sig`` is ignored entirely elsewhere — there is nothing to verify — but
    expiry is the one SAS property worth honouring, since it is what makes
    the renewal flow (re-sign, stage under SAS B, commit blocks staged
    under SAS A) reachable at all without real credentials.
    """
    raw = query.get("se")
    if not raw:
        return False
    expiry = datetime.strptime(raw, SAS_TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= expiry


def create_app(root=None) -> FastAPI:
    """Build the devblob ASGI app.

    A factory, like ``uploader/api/app.py``'s, so tests can point it at an
    isolated temporary directory.
    """
    store = DevBlobStore(root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Fail fast if the root cannot be created or written, rather than
        # failing on the first upload (§7 of the task brief).
        store.ensure_ready()
        logger.warning(
            "Dev blob store writing to %s — this is not Azure and must "
            "never be reachable in production.", store.root)
        yield

    app = FastAPI(title="Uploader dev blob store", lifespan=lifespan)
    app.state.store = store

    dev_origin = os.environ.get(ENV_DEV_ORIGIN) or DEFAULT_DEV_ORIGIN
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[dev_origin],
        allow_methods=["PUT", "OPTIONS"],
        allow_headers=CORS_ALLOWED_HEADERS,
        expose_headers=CORS_EXPOSED_HEADERS,
        max_age=CORS_MAX_AGE,
    )

    @app.put("/{container}/{blob_path:path}")
    async def put(container: str, blob_path: str, request: Request):
        try:
            validated_path = _validate_blob_path(container, blob_path)
        except (naming.InvalidPathError, naming.InvalidEmailError) as exc:
            return azure_error(400, "InvalidResourceName", str(exc))

        if _is_expired(request.query_params):
            return azure_error(
                403, "AuthenticationFailed", "Signature expired")

        comp = request.query_params.get("comp")

        if comp == COMP_BLOCK:
            block_id = request.query_params.get("blockid")
            if not block_id:
                return azure_error(
                    400, "MissingRequiredQueryParameter",
                    "blockid is required when comp=block")
            body = await request.body()
            store.stage_block(validated_path, block_id, body)
            return Response(status_code=201, headers={
                "x-ms-request-id": FAKE_REQUEST_ID,
            })

        if comp == COMP_BLOCKLIST:
            content_type = request.headers.get("x-ms-blob-content-type")
            xml_body = await request.body()
            try:
                block_ids = parse_block_list(xml_body)
            except ElementTree.ParseError:
                return azure_error(
                    400, "InvalidXmlDocument",
                    "Could not parse the block list")

            try:
                store.commit(validated_path, block_ids, content_type)
            except BlobAlreadyExistsError:
                return azure_error(
                    409, "BlobAlreadyExists",
                    "The specified blob already exists.")
            except UnknownBlockError as exc:
                return azure_error(
                    400, "InvalidBlockList",
                    f"Block {exc} was never staged")

            return Response(status_code=201, headers={
                "ETag": FAKE_ETAG,
                "x-ms-request-id": FAKE_REQUEST_ID,
            })

        return azure_error(
            400, "InvalidQueryParameterValue",
            f"Unrecognised comp={comp!r}")

    return app


app = create_app()

__all__ = ["app", "create_app", "azure_error", "parse_block_list"]

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        app,
        host=os.environ.get("UPLOADER_LOCAL_BLOB_HOST", DEFAULT_HOST),
        port=int(os.environ.get("UPLOADER_LOCAL_BLOB_PORT", DEFAULT_PORT)),
    )
