"""Environment configuration for the uploader service.

Every setting arrives as an environment variable. In production that means
the systemd unit and — for the Azure identity — an ``EnvironmentFile``
outside the repo (§3 of ``uploader/spec/tasks/2_azure_resources.md``); no
credential has a default. For local development, the same variables can be
supplied by an optional ``.env`` file at the uploader root (see
:data:`DOTENV_PATH`) — gitignored, never committed, and loaded with
``python-dotenv``. A real environment variable always wins: ``load_dotenv``
does not override a value that is already set, so systemd's
``EnvironmentFile`` still has the final word in production even if a stray
``.env`` were ever present there.

The service principal authenticates with a **certificate**, not a client
secret: it was provisioned with ``az ad sp create-for-rbac --create-cert``
(see ``uploader/azure.md``). ``AZURE_CLIENT_CERTIFICATE_PATH`` points at a
PEM holding both the private key and the certificate. The PEM never enters
the repo — it lives at a path readable only by the service user.

The module has one rule: :func:`load_config` is called once at startup and
**raises** on anything missing or unparseable, so the service refuses to
start rather than 500-ing on the first user request (§4.8 of the task
brief).

The SAS issuer is selected here. The default is the real Azure issuer, so a
host that simply forgets ``UPLOADER_SAS_ISSUER`` gets a service that will
not start without credentials rather than one that silently hands out fake
capabilities. ``fake`` must be opted into explicitly.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Optional local-dev convenience, gitignored. Lives at the uploader root —
# alongside venv/ and azure.md — not inside api/, so the one file also
# covers az-cli resource creation (see ../azure.md) and doesn't need a
# second copy. A missing file is not an error — load_dotenv() simply does
# nothing — and any variable already set in the real environment is never
# overwritten by one from this file.
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

ISSUER_AZURE = "azure"
ISSUER_FAKE = "fake"
ISSUER_LOCAL = "local"
VALID_ISSUERS = (ISSUER_AZURE, ISSUER_FAKE, ISSUER_LOCAL)

ENV_ISSUER = "UPLOADER_SAS_ISSUER"
ENV_STORAGE_ACCOUNT = "AZURE_STORAGE_ACCOUNT"
ENV_STORAGE_CONTAINER = "AZURE_STORAGE_CONTAINER"
ENV_TENANT_ID = "AZURE_TENANT_ID"
ENV_CLIENT_ID = "AZURE_CLIENT_ID"
ENV_CLIENT_CERT_PATH = "AZURE_CLIENT_CERTIFICATE_PATH"
ENV_DATABASE_PATH = "UPLOADER_DB_PATH"
ENV_MAX_UPLOAD_BYTES = "UPLOADER_MAX_UPLOAD_BYTES"
ENV_SAS_TTL_SECONDS = "UPLOADER_SAS_TTL_SECONDS"
ENV_RATE_LIMIT_MAX = "UPLOADER_RATE_LIMIT_MAX"
ENV_RATE_LIMIT_WINDOW = "UPLOADER_RATE_LIMIT_WINDOW_SECONDS"
ENV_ALLOWED_EXTENSIONS = "UPLOADER_ALLOWED_EXTENSIONS"
ENV_ALLOWED_CONTENT_TYPES = "UPLOADER_ALLOWED_CONTENT_TYPES"
ENV_MAX_LIST_RESULTS = "UPLOADER_MAX_LIST_RESULTS"
ENV_MAX_RENEWALS = "UPLOADER_MAX_RENEWALS"
ENV_LOCAL_BLOB_ROOT = "UPLOADER_LOCAL_BLOB_ROOT"
ENV_LOCAL_BLOB_ENDPOINT = "UPLOADER_LOCAL_BLOB_ENDPOINT"

DEFAULT_DATABASE_PATH = "uploads.sqlite3"
DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024 * 1024
DEFAULT_SAS_TTL_SECONDS = 24 * 60 * 60
DEFAULT_RATE_LIMIT_MAX = 200
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60 * 60
DEFAULT_MAX_LIST_RESULTS = 1000
DEFAULT_MAX_RENEWALS = 10
DEFAULT_LOCAL_BLOB_ROOT = "/tmp/uploader-blobs"
DEFAULT_LOCAL_BLOB_ENDPOINT = "http://127.0.0.1:8004"

# Both allowlists are advisory only (§10 of the spec): a SAS constrains the
# blob, never its contents. They reject the obvious mistakes cheaply.
DEFAULT_ALLOWED_EXTENSIONS = (
    ".ab1",
    ".bam",
    ".bz2",
    ".csv",
    ".fa",
    ".fasta",
    ".fastq",
    ".fq",
    ".gz",
    ".json",
    ".sam",
    ".tar",
    ".tsv",
    ".txt",
    ".xlsx",
    ".zip",
)
DEFAULT_ALLOWED_CONTENT_TYPES = (
    "application/gzip",
    "application/json",
    "application/octet-stream",
    "application/x-gzip",
    "application/zip",
    "text/csv",
    "text/plain",
    "text/tab-separated-values",
)

# The blob endpoint is templated rather than configured: the account name is
# the only variable part, and accepting a whole URL from the environment
# would let a misconfiguration point SAS URLs at an arbitrary host.
BLOB_ENDPOINT_TEMPLATE = "https://{account}.blob.core.windows.net"


class ConfigError(Exception):
    """A required setting is missing, empty or unparseable.

    Raised at startup only. The service must not start in this state.
    """


@dataclass(frozen=True)
class Config:
    """Resolved, validated settings for one process."""

    issuer: str
    storage_account: str
    storage_container: str
    tenant_id: str
    client_id: str
    client_certificate_path: Path
    database_path: Path
    max_upload_bytes: int
    sas_ttl_seconds: int
    rate_limit_max: int
    rate_limit_window_seconds: int
    allowed_extensions: frozenset
    allowed_content_types: frozenset
    max_list_results: int
    max_renewals: int
    local_blob_root: Path
    local_blob_endpoint: str

    @property
    def blob_endpoint(self) -> str:
        """Return the account's blob service URL, without a trailing slash."""
        return BLOB_ENDPOINT_TEMPLATE.format(account=self.storage_account)

    def blob_url(self, blob_path: str) -> str:
        """Return the full https URL of one blob, without a SAS."""
        return f"{self.blob_endpoint}/{self.storage_container}/{blob_path}"

    def az_path(self, blob_path: str) -> str:
        """Return the ``az://`` path Nextflow expects for one blob.

        Built server-side, from the configured container, so a client
        cannot emit a path that points at the wrong container by
        concatenating a hardcoded name.
        """
        return f"az://{self.storage_container}/{blob_path}"


def _require(env: dict, name: str) -> str:
    """Return a non-empty environment value, or raise :class:`ConfigError`."""
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable {name} is missing or empty")
    return value


def _positive_int(env: dict, name: str, default: int) -> int:
    """Return a positive integer setting, or raise :class:`ConfigError`."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable {name} is not an integer: {raw!r}"
        ) from exc

    if value <= 0:
        raise ConfigError(
            f"Environment variable {name} must be positive, got {value}")

    return value


def _csv_set(env: dict, name: str, default: tuple) -> frozenset:
    """Return a lowercased set from a comma-separated setting."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return frozenset(default)

    items = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not items:
        raise ConfigError(
            f"Environment variable {name} is set but lists nothing")

    return frozenset(items)


def _certificate_path(env: dict) -> Path:
    """Return the service principal's PEM path, proven usable.

    Checked eagerly at startup rather than on the first SAS request: a
    missing or unreadable certificate is a deployment mistake, and the
    service must refuse to start rather than fail an upload much later with
    an error that points at Azure instead of at the filesystem.
    """
    path = Path(_require(env, ENV_CLIENT_CERT_PATH)).expanduser()

    if not path.is_file():
        raise ConfigError(
            f"{ENV_CLIENT_CERT_PATH} points at {path}, which is not a file")

    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise ConfigError(
            f"{ENV_CLIENT_CERT_PATH} points at {path}, which cannot be read: "
            f"{exc.__class__.__name__}") from exc

    # The PEM carries the private key. The deployed posture is 0600
    # www-data:www-data; 0640 root:www-data is also valid. Only
    # world-readable is refused — the check targets exposure, not a
    # particular ownership scheme.
    if path.stat().st_mode & stat.S_IROTH:
        raise ConfigError(
            f"The certificate at {path} is world-readable. It holds the "
            "service principal's private key — chmod 640 and set the group "
            "to the service user.")

    return path


def _local_blob_root(env: dict, issuer: str) -> Path:
    """Return the local filesystem blob store's root.

    Honoured only when ``issuer`` is :data:`ISSUER_LOCAL` — for every other
    issuer the setting has no effect, so a root that does not yet exist must
    not block startup of the azure or fake issuer. Only when the local
    issuer is actually selected is the directory proven writable, per §4.8
    of the task brief: a stand-in that cannot write must refuse to start,
    not fail on the first upload.
    """
    raw = (
        (env.get(ENV_LOCAL_BLOB_ROOT) or "").strip()
        or DEFAULT_LOCAL_BLOB_ROOT
    )
    path = Path(raw).expanduser()

    if issuer != ISSUER_LOCAL:
        return path

    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".uploader-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise ConfigError(
            f"{ENV_LOCAL_BLOB_ROOT} points at {path}, which could not be "
            f"created or written to: {exc.__class__.__name__}") from exc

    return path


def _local_blob_endpoint(env: dict) -> str:
    """Return the host the local issuer embeds in its SAS URLs."""
    raw = (env.get(ENV_LOCAL_BLOB_ENDPOINT) or "").strip()
    return raw or DEFAULT_LOCAL_BLOB_ENDPOINT


def load_config(env: dict = None) -> Config:
    """Read, validate and freeze the process configuration.

    Args:
        env: mapping to read from; defaults to ``os.environ`` after loading
            ``.env`` into it, if present. Injectable so the failure paths
            can be tested without mutating the process or touching disk.

    Raises:
        ConfigError: on any missing required value or unparseable number.
            The caller must let this propagate — a service that starts
            misconfigured is worse than one that will not start.
    """
    if env is None:
        load_dotenv(DOTENV_PATH)
        env = os.environ

    issuer = (env.get(ENV_ISSUER) or ISSUER_AZURE).strip().lower()
    if issuer not in VALID_ISSUERS:
        raise ConfigError(
            f"{ENV_ISSUER} must be one of {', '.join(VALID_ISSUERS)}, "
            f"got {issuer!r}")

    storage_account = _require(env, ENV_STORAGE_ACCOUNT)
    storage_container = _require(env, ENV_STORAGE_CONTAINER)

    if issuer == ISSUER_AZURE:
        tenant_id = _require(env, ENV_TENANT_ID)
        client_id = _require(env, ENV_CLIENT_ID)
        client_certificate_path = _certificate_path(env)
    else:
        tenant_id = ""
        client_id = ""
        client_certificate_path = Path("")

    database_path = Path(
        (env.get(ENV_DATABASE_PATH) or "").strip()
        or DEFAULT_DATABASE_PATH
    ).expanduser()

    return Config(
        issuer=issuer,
        storage_account=storage_account,
        storage_container=storage_container,
        tenant_id=tenant_id,
        client_id=client_id,
        client_certificate_path=client_certificate_path,
        database_path=database_path,
        max_upload_bytes=_positive_int(
            env, ENV_MAX_UPLOAD_BYTES, DEFAULT_MAX_UPLOAD_BYTES),
        sas_ttl_seconds=_positive_int(
            env, ENV_SAS_TTL_SECONDS, DEFAULT_SAS_TTL_SECONDS),
        rate_limit_max=_positive_int(
            env, ENV_RATE_LIMIT_MAX, DEFAULT_RATE_LIMIT_MAX),
        rate_limit_window_seconds=_positive_int(
            env, ENV_RATE_LIMIT_WINDOW, DEFAULT_RATE_LIMIT_WINDOW_SECONDS),
        allowed_extensions=_csv_set(
            env, ENV_ALLOWED_EXTENSIONS, DEFAULT_ALLOWED_EXTENSIONS),
        allowed_content_types=_csv_set(
            env, ENV_ALLOWED_CONTENT_TYPES, DEFAULT_ALLOWED_CONTENT_TYPES),
        max_list_results=_positive_int(
            env, ENV_MAX_LIST_RESULTS, DEFAULT_MAX_LIST_RESULTS),
        max_renewals=_positive_int(
            env, ENV_MAX_RENEWALS, DEFAULT_MAX_RENEWALS),
        local_blob_root=_local_blob_root(env, issuer),
        local_blob_endpoint=_local_blob_endpoint(env),
    )


def redact(config: Config) -> dict:
    """Return a log-safe view of the config.

    The certificate *path* is safe to log and useful when diagnosing a
    startup failure; the file's contents are never read here.
    """
    return {
        "issuer": config.issuer,
        "storage_account": config.storage_account,
        "storage_container": config.storage_container,
        "tenant_id": config.tenant_id,
        "client_id": config.client_id,
        "client_certificate_path": str(config.client_certificate_path),
        "database_path": str(config.database_path),
        "max_upload_bytes": config.max_upload_bytes,
        "sas_ttl_seconds": config.sas_ttl_seconds,
        "rate_limit_max": config.rate_limit_max,
        "rate_limit_window_seconds": config.rate_limit_window_seconds,
        "max_list_results": config.max_list_results,
        "max_renewals": config.max_renewals,
        "local_blob_root": str(config.local_blob_root),
        "local_blob_endpoint": config.local_blob_endpoint,
    }
