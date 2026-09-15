"""Issue user-delegation SAS tokens, and read back blob properties.

The Azure dependency is confined to this module, behind two narrow
protocols. At the time this module was first written, the container and
the service principal from ``uploader/spec/tasks/2_azure_resources.md``
did not exist, so no code that touched Azure could be run; keeping the SDK
here meant everything else — auth, validation, naming, persistence, the
error taxonomy, rate limiting — was testable against
:class:`FakeSasIssuer` regardless. Both now exist and the core create-only
assumption has since been verified against real Azure (see
``uploader/spec/tasks/4_operator_runbook.md``), but the separation still
holds: it is what let the rest of the service be tested without waiting on
either.

Two protocols rather than one. :class:`SasIssuer` is the interface §4.3 of
the task brief specifies; :class:`BlobReader` is what post-hoc
reconciliation (§10 of the spec) needs, and it is separate because a future
reconciliation worker needs the reader without needing the signer. Both
concrete classes implement both.

SAS parameters (§7 of the spec), all asserted in the tests:

===========  =================================================
Resource     single blob (``b``) — never the container
Permission   ``c`` (create) only. ``w`` would permit overwrite
Expiry       24 hours, from config
Start        5 minutes in the past, for clock skew
Protocol     https only
IP range     omitted — client IPs are unstable behind NAT
===========  =================================================

The delegation key is cached in process. Its maximum lifetime is seven days;
this module requests six and refreshes an hour before expiry, then retries
**once** on a signing failure with a freshly requested key. Once, not in a
loop: a genuine credential failure should surface as a 503, not as a
hot retry against Entra ID.
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import CertificateCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

import config as config_module

logger = logging.getLogger(__name__)

SAS_PROTOCOL_HTTPS = "https"
SAS_START_SKEW = timedelta(minutes=5)

# Azure caps a user delegation key at 7 days. Six leaves room for the clock
# skew allowance at both ends without the request being rejected outright.
DELEGATION_KEY_LIFETIME = timedelta(days=6)
DELEGATION_KEY_REFRESH_MARGIN = timedelta(hours=1)

FAKE_SAS_SIGNATURE = "fake-signature-not-valid-for-azure"
FAKE_SAS_VERSION = "2024-11-04"


class SasError(Exception):
    """A SAS could not be issued, or blob properties could not be read.

    Maps to HTTP 503: it means Azure or the service principal is the
    problem, not the caller.
    """


@dataclass(frozen=True)
class IssuedSas:
    """One signed, single-blob, create-only capability."""

    blob_url: str
    sas_token: str
    expires_at: datetime

    @property
    def url(self) -> str:
        """Return the blob URL with the SAS query string appended.

        This value is a bearer capability. It must reach the client that
        asked for it and nothing else — never a log line, never an error
        body.
        """
        return f"{self.blob_url}?{self.sas_token}"


@dataclass(frozen=True)
class BlobProperties:
    """The facts about a blob that reconciliation compares against."""

    size: int
    content_type: str
    last_modified: datetime = None


@dataclass(frozen=True)
class ListedBlob:
    """One blob as it exists in the container, right now."""

    blob_path: str
    size: int
    content_type: str
    last_modified: datetime


class SasIssuer(Protocol):
    """Signs a create-only SAS for exactly one blob."""

    def issue(self, blob_path: str, expiry: datetime) -> IssuedSas:
        """Return a SAS for ``blob_path`` valid until ``expiry``."""
        ...


class BlobReader(Protocol):
    """Reads a blob's real properties, server-side.

    Nothing the client reports is evidence (§4.5 of the task brief); this is
    where the verdict comes from.
    """

    def get_blob_properties(self, blob_path: str) -> BlobProperties:
        """Return the blob's properties, or ``None`` if it does not exist."""
        ...


class BlobLister(Protocol):
    """Lists the blobs under a prefix.

    Separate from :class:`SasIssuer` and :class:`BlobReader` for the same
    reason they are separate from each other: a future reconciliation
    worker needs to list a container without needing the ability to sign a
    SAS or read one blob's properties.
    """

    def list_blobs(self, prefix: str) -> list:
        """Return every blob whose name starts with ``prefix``."""
        ...


def sas_start_time(now: datetime = None) -> datetime:
    """Return the SAS start time: five minutes in the past."""
    return (now or datetime.now(timezone.utc)) - SAS_START_SKEW


class AzureUserDelegationSasIssuer:
    """The real issuer: Entra service principal, user-delegation SAS.

    Not a managed identity — Cloudgene runs on Nectar, which has no instance
    metadata endpoint to issue one against (§0 of
    ``uploader/spec/tasks/2_azure_resources.md``). The cost is a client
    secret with an expiry date.

    **Partially exercised against real Azure.** The renewal design's core
    assumption — that a second create-only SAS can commit blocks staged
    under a first — was confirmed manually on 2026-09-15 (see
    ``uploader/spec/tasks/4_operator_runbook.md``): two SAS tokens were
    issued for the same blob path with real credentials, a block staged
    under each, and both committed successfully using only the second
    SAS's authority. :meth:`get_blob_properties` and the full
    completion/reconciliation flow have not yet been run against real
    Azure. Parameter assembly for every method is also tested with the SDK
    mocked out.
    """

    def __init__(
        self,
        account_name: str,
        container_name: str,
        tenant_id: str,
        client_id: str,
        client_certificate_path: str,
        blob_service_client: BlobServiceClient = None,
    ):
        self.account_name = account_name
        self.container_name = container_name
        self.account_url = config_module.BLOB_ENDPOINT_TEMPLATE.format(
            account=account_name)

        self._explicit_client = blob_service_client
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_certificate_path = str(client_certificate_path)

        self._client = blob_service_client
        self._delegation_key = None
        self._delegation_key_expiry = None
        self._lock = threading.Lock()

    def _get_client(self) -> BlobServiceClient:
        """Return the blob service client, building it on first use."""
        if self._client is None:
            # The PEM holds both the private key and the certificate, as
            # produced by `az ad sp create-for-rbac --create-cert`. Read by
            # the SDK, never by this module — the key stays out of process
            # memory we control and out of any log line.
            credential = CertificateCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                certificate_path=self._client_certificate_path,
            )
            self._client = BlobServiceClient(
                account_url=self.account_url,
                credential=credential,
            )
        return self._client

    def _request_delegation_key(self, now: datetime):
        """Ask Azure for a fresh delegation key and cache it."""
        start = now - SAS_START_SKEW
        expiry = now + DELEGATION_KEY_LIFETIME

        try:
            key = self._get_client().get_user_delegation_key(
                key_start_time=start,
                key_expiry_time=expiry,
            )
        except AzureError as exc:
            raise SasError(
                "Could not obtain a user delegation key: "
                f"{exc.__class__.__name__}") from exc

        self._delegation_key = key
        self._delegation_key_expiry = expiry
        logger.info(
            "Obtained a user delegation key, expires %s", expiry.isoformat())
        return key

    def _delegation_key_for(self, now: datetime, force_refresh: bool = False):
        """Return a usable delegation key, refreshing when it is due.

        Refreshes an hour before expiry rather than at expiry, so a key
        never goes stale mid-request under normal operation. The
        ``force_refresh`` path is the single retry described in §4 of the
        spec.
        """
        with self._lock:
            due = (
                force_refresh
                or self._delegation_key is None
                or self._delegation_key_expiry is None
                or now >= (
                    self._delegation_key_expiry
                    - DELEGATION_KEY_REFRESH_MARGIN)
            )
            if due:
                return self._request_delegation_key(now)
            return self._delegation_key

    def _sign(self, key, blob_path: str, expiry: datetime, start: datetime):
        """Return the SAS query string for one blob.

        Every parameter here is load-bearing. ``BlobSasPermissions(
        create=True)`` renders as ``c``: create fails on an existing blob,
        whereas ``w`` would permit overwriting anything the SAS names.
        Calling ``generate_blob_sas`` rather than ``generate_container_sas``
        is what makes the resource a single blob (``sr=b``).
        """
        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container_name,
            blob_name=blob_path,
            user_delegation_key=key,
            permission=BlobSasPermissions(create=True),
            expiry=expiry,
            start=start,
            protocol=SAS_PROTOCOL_HTTPS,
        )

    def issue(self, blob_path: str, expiry: datetime) -> IssuedSas:
        """Sign a create-only, single-blob SAS valid until ``expiry``."""
        now = datetime.now(timezone.utc)
        start = sas_start_time(now)
        key = self._delegation_key_for(now)

        try:
            token = self._sign(key, blob_path, expiry, start)
        except Exception as first_error:
            # One retry with a freshly requested key, for the case where the
            # cached key went stale early (revoked, or the account's keys
            # rotated). Exactly one — a loop here would hammer Entra ID
            # while the real fault stayed invisible.
            logger.warning(
                "SAS signing failed (%s); refreshing the delegation key and "
                "retrying once", first_error.__class__.__name__)
            key = self._delegation_key_for(now, force_refresh=True)
            try:
                token = self._sign(key, blob_path, expiry, start)
            except Exception as exc:
                raise SasError(
                    "Could not sign a SAS after refreshing the delegation "
                    f"key: {exc.__class__.__name__}") from exc

        return IssuedSas(
            blob_url=f"{self.account_url}/{self.container_name}/{blob_path}",
            sas_token=token,
            expires_at=expiry,
        )

    def get_blob_properties(self, blob_path: str) -> BlobProperties:
        """Return the blob's real size and content type, or ``None``."""
        try:
            blob_client = self._get_client().get_blob_client(
                container=self.container_name,
                blob=blob_path,
            )
            properties = blob_client.get_blob_properties()
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise SasError(
                "Could not read blob properties: "
                f"{exc.__class__.__name__}") from exc

        content_settings = getattr(properties, "content_settings", None)
        return BlobProperties(
            size=properties.size,
            content_type=getattr(content_settings, "content_type", None),
            last_modified=getattr(properties, "last_modified", None),
        )

    def list_blobs(self, prefix: str) -> list:
        """Return every blob under ``prefix``, read from Azure directly.

        Container-scoped ``Storage Blob Data Contributor`` already permits
        ``List`` — no new role assignment, nothing for the operator to do.
        """
        try:
            container_client = self._get_client().get_container_client(
                self.container_name)
            blobs = container_client.list_blobs(name_starts_with=prefix)
            return [
                ListedBlob(
                    blob_path=blob.name,
                    size=blob.size,
                    content_type=getattr(
                        blob.content_settings, "content_type", None),
                    last_modified=blob.last_modified,
                )
                for blob in blobs
            ]
        except AzureError as exc:
            raise SasError(
                f"Could not list blobs: {exc.__class__.__name__}") from exc


LOCAL_STAGED_DIRNAME = ".staged"
LOCAL_META_SUFFIX = ".meta"


class LocalFileSasIssuer:
    """A stand-in backed by a real directory, for exercising the byte
    transfer offline (``uploader/spec/tasks/06-mock-azure.md``).

    ``FakeSasIssuer`` with its in-memory ``self._blobs`` dict replaced by
    the filesystem: a real ``uploader/devblob`` process (a separate ASGI
    app — see that module's README for why) writes committed blobs and
    ``.meta`` sidecars under ``root`` in response to the browser's
    ``BlockBlobClient`` calls, and this class reads them back the same way
    the real Azure issuer reads Azure. It does not import
    ``uploader/devblob`` and ``uploader/devblob`` does not import this
    module — each knows the on-disk layout independently, which is the
    price of ``uploader/api/**`` structurally never importing the dev-only
    process.

    Selected only by ``UPLOADER_SAS_ISSUER=local``. Never the default: a
    host that forgets the variable gets a service that will not start, not
    one that quietly writes to a local directory instead of Azure.
    """

    def __init__(
        self,
        account_name: str,
        container_name: str,
        root,
        endpoint: str,
    ):
        self.account_name = account_name
        self.container_name = container_name
        self.root = Path(root)
        self.endpoint = endpoint.rstrip("/")

    def issue(self, blob_path: str, expiry: datetime) -> IssuedSas:
        """Return a well-formed but unsigned SAS pointing at the local
        endpoint rather than Azure."""
        start = sas_start_time()
        token = "&".join([
            f"sv={FAKE_SAS_VERSION}",
            "sr=b",
            f"sp={BlobSasPermissions(create=True)}",
            f"st={_sas_timestamp(start)}",
            f"se={_sas_timestamp(expiry)}",
            f"spr={SAS_PROTOCOL_HTTPS}",
            f"sig={FAKE_SAS_SIGNATURE}",
        ])
        return IssuedSas(
            blob_url=f"{self.endpoint}/{self.container_name}/{blob_path}",
            sas_token=token,
            expires_at=expiry,
        )

    def _resolve(self, blob_path: str) -> Path:
        """Return ``blob_path`` joined onto ``root``, or ``None``.

        ``blob_path`` normally reaches this class only after
        ``naming.build_blob_path`` has already proven it cannot escape a
        user's prefix, but this is a belt-and-braces check of the same
        shape naming.py itself uses: refuse anything that would resolve
        outside ``root``, rather than trust every caller to have validated
        first.
        """
        candidate = (self.root / blob_path).resolve()
        root_resolved = self.root.resolve()
        escapes = (
            candidate != root_resolved
            and root_resolved not in candidate.parents)
        return None if escapes else candidate

    def get_blob_properties(self, blob_path: str) -> BlobProperties:
        """Return the real file's size, mtime and sidecar content type."""
        target = self._resolve(blob_path)
        if target is None or not target.is_file():
            return None

        content_type = None
        meta_path = target.with_name(target.name + LOCAL_META_SUFFIX)
        if meta_path.is_file():
            try:
                content_type = json.loads(
                    meta_path.read_text()).get("content_type")
            except (OSError, ValueError):
                content_type = None

        stat_result = target.stat()
        return BlobProperties(
            size=stat_result.st_size,
            content_type=content_type,
            last_modified=datetime.fromtimestamp(
                stat_result.st_mtime, tz=timezone.utc),
        )

    def list_blobs(self, prefix: str) -> list:
        """Walk the root, excluding staged blocks and ``.meta`` sidecars.

        Azure does not list a blob until its blocks are committed, so the
        ``.staged/`` tree — where ``uploader/devblob`` keeps uncommitted
        blocks — is excluded here the same way it would be absent from a
        real container listing.
        """
        if not self.root.is_dir():
            return []

        results = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue

            relative = path.relative_to(self.root)
            if relative.parts and relative.parts[0] == LOCAL_STAGED_DIRNAME:
                continue

            blob_path = relative.as_posix()
            if blob_path.endswith(LOCAL_META_SUFFIX):
                continue
            if not blob_path.startswith(prefix):
                continue

            properties = self.get_blob_properties(blob_path)
            results.append(ListedBlob(
                blob_path=blob_path,
                size=properties.size,
                content_type=properties.content_type,
                last_modified=properties.last_modified,
            ))

        return sorted(results, key=lambda blob: blob.blob_path)


class FakeSasIssuer:
    """A stand-in for tests and for local development without credentials.

    It emits a query string with the same parameter names and the same
    values as the real one — ``sp=c``, ``sr=b``, ``spr=https`` — so a test
    that asserts on them is asserting on something meaningful, but with a
    signature that Azure will reject. It also keeps an in-memory blob table
    so reconciliation can be exercised end to end.

    Selected only by ``UPLOADER_SAS_ISSUER=fake``. It is never the default:
    a host that forgets the variable gets a service that will not start, not
    one that hands out unusable SAS URLs that look fine.
    """

    def __init__(self, account_name: str, container_name: str):
        self.account_name = account_name
        self.container_name = container_name
        self.account_url = config_module.BLOB_ENDPOINT_TEMPLATE.format(
            account=account_name)
        self.issued = []
        self._blobs = {}

    def issue(self, blob_path: str, expiry: datetime) -> IssuedSas:
        """Return a well-formed but unsigned SAS for one blob."""
        start = sas_start_time()
        token = "&".join([
            f"sv={FAKE_SAS_VERSION}",
            "sr=b",
            f"sp={BlobSasPermissions(create=True)}",
            f"st={_sas_timestamp(start)}",
            f"se={_sas_timestamp(expiry)}",
            f"spr={SAS_PROTOCOL_HTTPS}",
            f"sig={FAKE_SAS_SIGNATURE}",
        ])
        issued = IssuedSas(
            blob_url=f"{self.account_url}/{self.container_name}/{blob_path}",
            sas_token=token,
            expires_at=expiry,
        )
        self.issued.append(issued)
        return issued

    def put_blob(
        self,
        blob_path: str,
        size: int,
        content_type: str = None,
        last_modified: datetime = None,
    ) -> None:
        """Pretend a client finished uploading to ``blob_path``."""
        self._blobs[blob_path] = BlobProperties(
            size=size,
            content_type=content_type,
            last_modified=last_modified or datetime.now(timezone.utc),
        )

    def get_blob_properties(self, blob_path: str) -> BlobProperties:
        """Return the pretend blob's properties, or ``None``."""
        return self._blobs.get(blob_path)

    def list_blobs(self, prefix: str) -> list:
        """Return every pretend blob whose name starts with ``prefix``."""
        return sorted(
            (
                ListedBlob(
                    blob_path=path,
                    size=properties.size,
                    content_type=properties.content_type,
                    last_modified=properties.last_modified,
                )
                for path, properties in self._blobs.items()
                if path.startswith(prefix)
            ),
            key=lambda blob: blob.blob_path,
        )


def _sas_timestamp(value: datetime) -> str:
    """Return the SAS wire format for a timestamp."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_issuer(config) -> SasIssuer:
    """Return the issuer selected by configuration.

    Raises:
        SasError: the configured issuer name is not recognised. Unreachable
            from :func:`config.load_config`, which validates the name, but
            kept so the mapping fails closed if it is called directly.
    """
    if config.issuer == config_module.ISSUER_FAKE:
        logger.warning(
            "Using the FAKE SAS issuer; the URLs it returns will be rejected "
            "by Azure. This must not be the configuration in production.")
        return FakeSasIssuer(
            account_name=config.storage_account,
            container_name=config.storage_container,
        )

    if config.issuer == config_module.ISSUER_LOCAL:
        logger.warning(
            "Using the LOCAL filesystem SAS issuer; uploads land under %s "
            "on this host, not Azure. This must not be the configuration "
            "in production.", config.local_blob_root)
        return LocalFileSasIssuer(
            account_name=config.storage_account,
            container_name=config.storage_container,
            root=config.local_blob_root,
            endpoint=config.local_blob_endpoint,
        )

    if config.issuer == config_module.ISSUER_AZURE:
        return AzureUserDelegationSasIssuer(
            account_name=config.storage_account,
            container_name=config.storage_container,
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_certificate_path=config.client_certificate_path,
        )

    raise SasError(f"Unknown SAS issuer {config.issuer!r}")
