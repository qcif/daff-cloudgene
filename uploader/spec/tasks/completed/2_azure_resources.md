# Task 2 — Azure resources to be created

**Status:** awaiting action by the operator. The storage account exists; the
container and the application identity do not.
**Owner:** Cameron (has Azure access; the investigation agent does not)
**Blocks:** §4 and §8 of [client-azure-upload.md](../client-azure-upload.md) —
nothing about SAS issuance can be built or tested until the identity exists.

This is the list of things to create, and the decisions that need making first.

## 0. Settled: the uploader cannot use a managed identity

**Cloudgene runs on Nectar, not Azure.** It dispatches Nextflow work to Azure
Batch, but the FastAPI uploader itself executes on the Nectar VM.

This matters because **a managed identity is not a portable credential.** It is
usable only *from Azure compute*: the platform issues tokens through the
instance metadata endpoint (`169.254.169.254`), which exists only on Azure VMs,
Batch nodes, App Service, and so on. A process on a Nectar VM has no way to
authenticate *as* a managed identity — there is nothing to present and no
endpoint to present it to.

So the existing managed identity, presumably attached to the Azure Batch pool
nodes so Nextflow tasks can reach storage, remains correct for that job and
cannot be reused here. The uploader needs its own credential:

| | **Managed identity** | **Service principal** (what we need) |
|---|---|---|
| Usable from | Azure compute only | Anywhere, including Nectar |
| Credential on the host | None — platform-issued | Client secret or certificate |
| Rotation | Automatic | Manual, and it expires |
| Works for user-delegation SAS | Yes | Yes |

The user-delegation SAS design in §4 of the spec is unaffected —
`generateUserDelegationKey` comes with the Storage Blob Data Contributor role
either way. The only change is that the uploader holds a real secret, which
brings an expiry date and a rotation obligation (§3).

If a managed identity is ever wanted for this service, the prerequisite is
moving the uploader onto Azure compute — a much larger change, and not worth
making for this reason alone.

## 1. Storage account (exists — please confirm details)

Needed before anything can be configured:

- **Account name** and **resource group** (the account name becomes part of
  every blob URL: `https://<account>.blob.core.windows.net/...`).
- **Region** — ideally the same region as the compute doing any later
  processing, for egress cost and latency.
- **Kind and tier** — confirm StorageV2. Confirm whether **hierarchical
  namespace (ADLS Gen2)** is enabled, because it changes path semantics for the
  `user-email/` prefixes in §7 of the spec.
- **Minimum TLS version 1.2** and **"Secure transfer required" enabled**.
- **Public network access** — the browser uploads directly to the blob endpoint,
  so the account must be reachable from client networks. If a private endpoint
  or an IP firewall is in place, direct browser upload will not work as
  designed; flag it now rather than at integration time.
- **Whether "Allow shared key access" can be disabled.** The design uses
  user-delegation SAS only, so disabling account-key access is a free hardening
  win — but confirm nothing else depends on the account key first.

## 2. Container (to create)

- **One container** for uploads. Suggested name: `uploads`.
- **Public access level: Private.** No anonymous read, ever. The SAS is the only
  access path for clients.
- **Blob soft delete** — recommended, 7–30 days, so a mistaken overwrite or
  delete is recoverable.
- **Uncommitted blocks need no rule — Azure reaps them itself.** An earlier
  draft of this document called for a lifecycle rule to delete uncommitted
  blocks. That was wrong: Azure garbage-collects uncommitted blocks
  automatically one week after the last successful `Put Block`, and lifecycle
  management has no action that targets them in any case. The "user closes tab
  mid-upload" case in §12 of the spec is handled by the platform. **No action
  required.**

- **The orphans that *do* persist are committed blobs.** A client that finishes
  its `PUT` but never calls the completion endpoint — or whose upload fails
  validation — leaves a real, billable blob behind with a record stuck at
  `pending` or `failed`. This is the case actually worth cleaning up, and it is
  best done by the uploader itself rather than by a lifecycle rule, because the
  service already knows which uploads those are (see §2.1).
- **Versioning** — decide. The SAS grants `create` only, not overwrite (§7 of
  the spec), so versioning is not required to prevent clobbering. Leave it off
  unless there is a separate reason.

### 2.1 Cleaning up abandoned committed blobs

The uploader's `expire_pending()` sweep already identifies records that lapsed
without a completion callback. Extend it to **delete the corresponding blob**
when it marks a record `expired`, and likewise for records that fail
validation. `Storage Blob Data Contributor` on the container already includes
delete, so no role change is needed.

This is preferable to a lifecycle rule because it is precise: it removes exactly
the blobs known to be abandoned, on the service's own state, and never touches a
legitimately completed upload.

**A blanket lifecycle rule is the wrong tool here until one question is
answered:** whether Azure Batch consumes uploaded files *in place* under
`container/<user-email>/...`, or copies them into a per-job location first
(§3.1). If files are read in place, an age-based delete rule would eventually
remove live inputs. Settle §3.1 before adding any time-based retention.

## 3. Service principal for the uploader (to create)

Per §0 this must be an Entra **app registration**, not a managed identity.

- Create the app registration. A dedicated one for the uploader — do not reuse
  the Batch identity, so that the two can be revoked independently and the audit
  trail distinguishes "a user uploaded" from "a pipeline read".
- **Settled: a certificate, not a client secret.** Created with
  `az ad sp create-for-rbac --create-cert --years 3`; the PEM lives at
  `/etc/cloudgene-uploader/azure-cert.pem`, mode 600, owned by the service user.
  Rotation and revocation commands are in [azure.md](../../azure.md).

  Three years is long enough to be forgotten, so **record the expiry somewhere
  that will actually be seen** — a calendar reminder, not a comment. An expired
  credential takes uploads down with no warning and an error that does not
  obviously say "expired".
- **Two role assignments are needed, at different scopes** — this was an open
  ambiguity and is now settled (see [azure.md](../../azure.md)):

  | Role | Scope | Why |
  |---|---|---|
  | `Storage Blob Delegator` | **Storage account** | `generateUserDelegationKey` is an account-level operation; a container-scoped assignment does not grant it |
  | `Storage Blob Data Contributor` | **Container** | Read blob properties for the §10 post-hoc validation, keeping data access as narrow as possible |

  The earlier draft assumed container-scoped Data Contributor alone would cover
  both. It does not — the delegation key would fail at runtime with an
  authorisation error that looks nothing like a missing role.
- Provide `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and the **certificate path** to
  the service. These go in an `EnvironmentFile` outside the repo, readable only
  by `www-data` — never committed, and never inline in the systemd unit, which
  is world-readable. The certificate itself must be readable by the service
  user and by nobody else.

### 3.1 Batch also needs access to this container

The point of these uploads is that a workflow consumes them, so whatever
identity the Azure Batch pool runs as needs **read** access to the same
container — `Storage Blob Data Reader` on the container is sufficient if the
pipeline only reads inputs from here.

Worth settling now rather than at integration time: whether uploaded files are
read by Batch *in place* under `container/user-email/...`, or copied into a
per-job location first. It affects nothing in the uploader's design, but it
decides whether the `user-email/` prefix scheme in §7 of the spec has to be
legible to the Nextflow side as well.

## 4. CORS on the Blob service (to configure)

Configured **on the storage account**, not in FastAPI — FastAPI's CORS
middleware has no bearing on requests the browser makes to
`*.blob.core.windows.net`. Per §8 of the spec:

| Setting | Value |
|---|---|
| Allowed origins | `https://cloudgene.qcif.edu.au` — exactly this, not `*` |
| Allowed methods | `PUT`, `OPTIONS` (add `GET`/`HEAD` only if the client reads blobs directly) |
| Allowed headers | `x-ms-blob-type`, `x-ms-blob-content-type`, `content-type`, `content-length`, **`x-ms-version`, `x-ms-client-request-id`** |
| Exposed headers | `etag`, `x-ms-request-id` |
| Max age | 3600 |

A failed preflight surfaces in the browser as an opaque network error with no
useful detail, so this is worth verifying in isolation before debugging any
upload logic.

> **Escalated 2026-09-15, from
> [`06-mock-azure.md`](../06-mock-azure.md) §4:** the Allowed headers row
> above is missing headers. Measuring the actual requests the bundled SDK
> issues (`BlockBlobClient` against a plain HTTP host) showed every `PUT`
> carrying `x-ms-version` and `x-ms-client-request-id`, and both appear in
> the preflight's `Access-Control-Request-Headers`. Neither was in the
> original list above, which as written would fail preflight in
> production — surfacing as exactly the opaque network error warned about
> two paragraphs up. **A live end-to-end run against a real browser (the
> §9 verification of `06-mock-azure.md`) turned up a third, missed for the
> same reason: `x-ms-useragent`** — client telemetry (SDK, pipeline and
> browser versions) the SDK also sets on every request unconditionally.
> Static measurement of the SDK's outgoing calls did not catch it; only
> driving a real preflight in a real browser did. **All three bolded
> headers need to be added to the Allowed headers list when this rule is
> applied.** The local dev stand-in (`uploader/devblob/`) already accepts
> the full set, so this only affects the real storage account, which has
> not had this rule applied yet.

## 5. Network egress from the Cloudgene server

The FastAPI service needs outbound HTTPS to:

- `login.microsoftonline.com` — to authenticate and obtain tokens
- `<account>.blob.core.windows.net` — to request the user delegation key and to
  read blob properties during validation

Confirm the Nectar VM's egress rules permit these. Cloudgene already talks to
Azure Batch from this host, so outbound HTTPS to Azure is evidently open in
general — but confirm the blob and login endpoints specifically rather than
assuming the Batch path covers them.

## 6. Deferred: BlobCreated events

§3 of the spec shows validation triggered by Event Grid `BlobCreated` events,
which would require an Event Grid subscription and a publicly reachable webhook
endpoint on the uploader. **Do not create this yet.** The simpler path —
validating on the client's completion callback, and reconciling against the
pending-upload record — covers the same ground for a closed, authenticated user
base and needs no new inbound surface. Revisit only if client-reported
completion proves unreliable in practice.

## 7. What to report back

Once created, the uploader needs:

- Storage account name and container name
- Tenant ID, client ID, and the client secret (delivered out of band, not in
  this repo)
- The secret's expiry date
- Confirmation the role assignment is on the container scope
- Confirmation that the Batch identity can read the container (§3.1)
- Confirmation that CORS is applied and egress is open
