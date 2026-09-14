# Direct browser-to-Azure Blob Storage uploads

**Stack:** Vue.js SPA frontend, FastAPI backend, Azure Blob Storage.

**Deployment context:** this is an auxiliary service to the existing production
Cloudgene instance at `cloudgene.qcif.edu.au` (see [downloader/app.py](../../downloader/app.py)
and [config/nginx-vhost.conf](../../config/nginx-vhost.conf) for the established
pattern: a small FastAPI app on a local port, reverse-proxied by nginx under a
sub-path, run by systemd as `www-data`). It is **not** a standalone site: it is
served under a sub-path of the Cloudgene origin (§6) and authenticates users by
reusing their existing Cloudgene session (§5).

## 1. Purpose

Allow an authenticated user's browser to upload files directly to Azure Blob
Storage, without the file payload passing through the FastAPI application.

This removes large uploads from the application server's request path: no
request-size limits to tune, no worker tied up for the duration of a transfer,
no timeout ceiling on how long an upload may take. It also decouples upload
availability from backend availability once a token has been issued.

### Non-goals

- Client-side resumability **across browser sessions**. An upload is abandoned
  and restarted if the page is closed or reloaded.

  Resumability *within* a session — surviving a network interruption or a SAS
  that expires mid-transfer — is in scope (2026-09-15). It is delivered by
  staging blocks explicitly and tracking the staged IDs in memory; see §6.1 of
  [02-client.md](02-client.md). The session boundary is where it stops because
  recovering the staged block list from Azure would require `r` on the SAS, and
  create-only is worth more than the feature.

## 2. Core principle

**Azure account credentials are never sent to the browser.** The account key,
connection string, and the service principal's credentials stay server-side. The
browser receives only a Shared Access Signature (SAS): a signed, scoped,
short-lived capability to write to one specific blob path.

A SAS is a bearer capability. Anyone holding the URL can use it until it
expires. Every constraint below follows from that fact.

## 3. Architecture

```
Browser (Vue)          nginx        FastAPI      Cloudgene       Azure
                                              (127.0.0.1:8082)
     |                   |             |             |             |
     |-- POST /uploads/api/uploads --->|             |             |
     |   + X-Auth-Token (from          |             |             |
     |     Cloudgene localStorage)     |             |             |
     |                   |             |- forward -->|             |
     |                   |             |  token to   |             |
     |                   |             |  /api/v2/   |             |
     |                   |             |  server     |             |
     |                   |             |<-- mail, ---|             |
     |                   |             |    apps     |             |
     |                   |             |- get user delegation key >|
     |                   |             |  (service principal)      |
     |                   |             |<--------------------------|
     |                   |             |  sign SAS for one blob    |
     |<-- blob URL + SAS --------------|             |             |
     |                   |             |             |             |
     |------- PUT file bytes directly (browser -> Azure) --------->|
     |                   |             |             |             |
     |                   |             |<-- BlobCreated event -----|
     |                   |             |    (via Event Grid)       |
     |                   |             |  validate, promote        |
```

Only JSON control-plane traffic passes through nginx and FastAPI; the file bytes
go straight from the browser to `*.blob.core.windows.net`, so nginx's
`client_max_body_size` and proxy timeouts do not constrain the upload.

## 4. Credential model

Use a **user delegation SAS** in preference to an account-key SAS.

| | Account key SAS | User delegation SAS |
|---|---|---|
| Signing secret | Storage account key, held by the app | Delegation key obtained from Entra ID |
| Key present in app config | Yes | No |
| Revocation | Rotate the account key (breaks everything) | Revoke the delegation key (invalidates SAS signed with it) |
| Audit trail | Attributed to the account | Attributed to the signing identity |

The FastAPI service authenticates to Azure with an Entra **service principal**,
granted the **Storage Blob Data Contributor** role on the target container. It
requests a user delegation key, caches it for its lifetime, and signs SAS tokens
with it.

> **Not a managed identity.** Cloudgene runs on Nectar, not Azure, and a managed
> identity is only usable from Azure compute — there is no metadata endpoint on
> a Nectar VM to issue tokens against. (The managed identity already in use for
> the Azure Batch pool is correct for that job and cannot be reused here.) The
> user-delegation SAS design is unaffected: `generateUserDelegationKey` comes
> with the same role either way. The cost is that the service holds a client
> secret with an expiry date — see §0 and §3 of
> [tasks/2_azure_resources.md](tasks/2_azure_resources.md).

The delegation key has a maximum lifetime of seven days. Cache it in process
with a refresh well before expiry, and handle the case where a cached key has
gone stale by re-requesting and retrying once.

## 5. Authentication and authorisation (Cloudgene integration)

The uploader has **no login of its own**. A user logs into Cloudgene as normal
and the uploader reuses that session, by forwarding the user's Cloudgene token
back to Cloudgene and reading the answer.

> The mechanism below was established from a production capture; see
> [tasks/1_investigate_user_auth.md](tasks/1_investigate_user_auth.md) for the
> evidence and the verification still outstanding.

### 5.1 The credential is a header, not a cookie

Cloudgene authenticates with an **`X-Auth-Token` request header** carrying a
JWT. There is no session cookie — the only cookie on the origin is a
cookie-consent artefact. **Nothing is sent to the uploader automatically.**

The browser-side flow:

1. Cloudgene stores the token in `localStorage` under the key `cloudgene`, as
   `{"token": "eyJhbGci..."}`.
2. `localStorage` is scoped to the **origin**, not the path, so the uploader's
   app at `/uploads/` reads the same store as Cloudgene at `/`. This is the
   reason same-origin deployment (§6) is a requirement, not a convenience.
3. The uploader's frontend reads the token **per request** (not once at load, so
   that a renewed token is picked up) and sets it as `X-Auth-Token` on every
   call to its own API.
4. No token, an unparseable value, or a `401` from the API means redirect to the
   Cloudgene login with a return URL.

A consequence worth preserving: because the credential is a custom header rather
than an ambient cookie, the uploader's API is inherently **CSRF-resistant**. Do
not migrate it to cookie auth.

### 5.2 Validation: forward the token to Cloudgene

The backend does not parse the JWT for identity. It forwards the token over
loopback and treats Cloudgene's answer as authoritative:

```
GET http://127.0.0.1:8082/api/v2/server
X-Auth-Token: <the client's token, passed through unmodified>
```

The response carries both facts the uploader needs:

```json
{
  "user": { "username": "...", "mail": "user@example.com", "admin": false },
  "apps": [ { "id": "taxodactyl_150@1.5.0", "name": "Taxodactyl", ... } ],
  "loggedIn": true
}
```

- **Authenticated** iff `loggedIn == true`. Note this endpoint returns `200`
  with `loggedIn: false` for an absent or invalid token, so a `200` is **not**
  evidence of authentication — parse the body and fail closed on anything
  unexpected.
- **Authorised** iff `apps` is non-empty.

  The policy this implements: **access to any workflow is the authorisation.**
  Untrusted users are not granted access to any workflow in the first place, so
  a user who can run one has already been approved, and uploading data for it is
  within that approval. No per-app matching, and no role list to keep in sync
  with Cloudgene — a `^taxodactyl` pattern would wrongly deny someone approved
  only for `nanopore-assembly`.

  `apps` is filtered by entitlement, confirmed: a logged-in user without the
  appropriate role receives an empty list, as does an anonymous caller. The
  check is therefore a real gate, not a proxy for one.
- **Identity** is `user.mail` — the blob prefix (§7). The JWT's `sub` is the
  *username*, not the email, so the token alone is insufficient even if it were
  trusted.

  **An account may have no email.** Cloudgene's own UI offers to "enter your
  email address at any time to upgrade your account", so `user.mail` can be
  absent or null for a legitimately logged-in, possibly entitled user. Since the
  email *is* the storage prefix, such a user cannot be given a SAS: return `403`
  with a message telling them to set an email in Cloudgene. Do not treat it as a
  malformed response — it is an expected state, and reporting it as a `503`
  would make a user-fixable problem look like an outage.

**Why not verify the JWT locally?** The token is HS256-signed, so local
verification would require a copy of Cloudgene's signing secret — a credential
that can mint any user's identity — and it still would not yield the email.
Forwarding costs one loopback request and keeps every policy decision in
Cloudgene. See §6 of the task doc for the full comparison.

### 5.3 Design constraints

- **Never trust client-supplied identity.** No email, username, role or app id
  from the request body is used for anything. The JWT payload is readable
  without the key, so it too is untrusted input: never parse it for identity.
  The only thing the uploader takes from the client is the opaque token, and the
  only thing it forwards to Cloudgene is that token.
- **Validate on every token-issuance request, and cache nothing.** Group
  membership can change and sessions end. An earlier draft allowed a ≤60 s TTL
  keyed on the token; that allowance is **withdrawn** (2026-09-14). Loopback
  validation was measured at a median of **6.6 ms**, against 62 ms over public
  HTTPS — see §B4 of [tasks/1_operator_runbook.md](tasks/1_operator_runbook.md).
  At that cost a cache buys nothing measurable and delays revocation by up to a
  minute. Since `apps` reflects *live* entitlement rather than the token's
  `roles` claim, removing someone's access takes effect on their very next
  request; a cache is the only thing that would reintroduce a revocation lag.
  The implementation has no identity cache.
- **Fail closed.** Any error resolving identity — Cloudgene unreachable,
  malformed response, missing `loggedIn` — is a `401`/`403`/`503`, never a
  default-allow.
- **Startup self-test.** `/api/v2/server` is a frontend-facing API, not a
  documented integration point; an upgrade can change its shape. Assert the
  expected response shape at startup and fail loudly rather than degrading to
  allow-all.
- **No Cloudgene database access.** The uploader holds no Cloudgene credentials
  and no signing secret. Its own pending-upload records (§7) live in its own
  store.
- **Token lifetime is 24 hours** and logout may be client-side only, so a token
  can outlive the user's session in the browser. This is Cloudgene's existing
  posture; the uploader inherits it rather than worsening it.

## 6. Deployment: serving under a sub-path

The app is served under `https://cloudgene.qcif.edu.au/uploads/`. Same-origin
placement is what makes §5 work at all: `localStorage` is partitioned by origin,
so a separate hostname could not read the Cloudgene token and the user would
have to log in twice.

Following the existing `/validation/` pattern in
[config/nginx-vhost.conf](../../config/nginx-vhost.conf):

```nginx
# Azure uploader API
location /uploads/api/ {
    proxy_pass http://127.0.0.1:8003/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Azure uploader SPA
location /uploads/ {
    alias /mnt/data/.../uploader/client/dist/;
    try_files $uri $uri/ /uploads/index.html;
}
```

Consequences to get right:

- **The `location /` Cloudgene proxy must not shadow these.** nginx prefix
  matching picks the longest match, so `/uploads/` wins over `/`; no ordering
  change is needed, but the new blocks must be added above the Cloudgene block
  for readability and to match the existing file.
- **Port.** Cloudgene is on `8082`, the validation API on `8000`, the downloader
  on `8002`. Use `8003` for the uploader and bind to `127.0.0.1` only.
- **Trailing slash on `proxy_pass`.** `http://127.0.0.1:8003/` strips the
  `/uploads/api` prefix, so FastAPI sees `/uploads` for
  `POST /uploads/api/uploads`. Set `root_path="/uploads/api"` on the FastAPI app
  so generated URLs and the OpenAPI docs are correct.
- **Vue base path.** Build with `base: '/uploads/'` in `vite.config.js`, or all
  asset URLs resolve against `/` and 404 through the Cloudgene proxy. If the
  router is used at all, it needs the same base.
- **`localStorage` access, not cookie scope.** `localStorage` is keyed on the
  origin alone, so any path under `cloudgene.qcif.edu.au` can read the token
  (§5.1). Nothing about the deployment path needs to change for auth to work —
  but a move to a separate hostname, or to a different port, would break it.
- **No CORS needed for the API.** Browser-to-FastAPI is same-origin. CORS in §8
  applies only to browser-to-Azure requests. Note the app must not be made
  cross-origin later "for convenience": that would break the token read *and*
  require CORS with credentials.
- **Unauthenticated users** hitting `/uploads/` should be redirected to the
  Cloudgene login page with a return URL, not shown a broken app.
- **systemd unit** modelled on
  [downloader/downloads.service](../../downloader/downloads.service): `www-data`,
  `Restart=on-failure`, the same hardening block, and the Azure and Cloudgene
  DB settings supplied as environment variables (or an `EnvironmentFile` outside
  the repo — no secrets committed).

## 7. Backend: token issuance endpoint

**`POST /uploads/api/uploads`** — authenticated via the Cloudgene session (§5).

**Request:** original filename, declared byte size, declared content type.

**Server-side checks before issuing:**

- The token resolves to a logged-in Cloudgene user with access to at least one
  workflow (§5).
- The declared size is within the accepted range. This is advisory only (see
  §10) but rejects the obvious cases cheaply.
- The declared content type and filename extension are in the allowlist. Also
  advisory.
- The user is within their rate/quota budget for token issuance.

**Server-side blob naming.** The client generates the blob name, but it is
placed in a folder designated by the server as `container/user-email/$path`,
where `user-email` is the email resolved from the Cloudgene user record (§5) —
never a value supplied by the client. Everyone who gets this far holds access to
at least one workflow and is therefore an approved user (§5), which is what makes
a shared-container, prefix-separated layout acceptable here — the trust comes
from the gate, not from the system merely being closed. Normalise the email
(lowercase) and reject client paths containing `..` or leading `/` so the prefix
cannot be escaped.

**SAS parameters:**

| Parameter | Value | Rationale |
|---|---|---|
| Resource | Single blob (`b`), never container (`c`) | A container-scoped SAS grants write access to every blob in it |
| Permissions | `c` (create) only | `create` fails on an existing blob; `w` (write) would permit overwrite |
| Expiry | 24 hours | Long enough for a slow connection, short enough to limit exposure |
| Start time | Omitted, or 5 minutes in the past | Guards against clock skew between the signing host and Azure |
| Protocol | HTTPS only | |
| IP range | Omit | Client IPs are unstable behind mobile networks and corporate NAT |

**Response:** the full blob URL with SAS query string, the expiry timestamp, and
an opaque upload identifier that the client uses when calling back to the
backend.

Persist a pending-upload record server-side at issuance time: upload ID, user,
blob path, declared size and type, issued-at and expiry. This record is what
post-hoc validation (§10) reconciles the real blob properties against. It lives
in the uploader's own store; the uploader never touches Cloudgene's database
(§5.3).

## 8. Storage account CORS

> This must be actioned by a human — see §4 of
> [tasks/2_azure_resources.md](tasks/2_azure_resources.md), which lists the
> exact settings alongside the other Azure resources still to be created.

CORS for direct uploads is configured **on the storage account**, not in the
FastAPI application. FastAPI's CORS middleware has no bearing on requests the
browser makes to `*.blob.core.windows.net`.

Configure a CORS rule on the Blob service:

- **Allowed origins:** the exact SPA origins, one per environment. Not `*` —
  with `*`, any site can drive uploads with a leaked SAS from a victim's browser.
- **Allowed methods:** `PUT` and `OPTIONS`. Add `GET` and `HEAD` only if the
  frontend also reads blobs directly.
- **Allowed headers:** `x-ms-blob-type`, `x-ms-blob-content-type`,
  `content-type`, `content-length`. The SDK sets these.
- **Exposed headers:** `etag`, `x-ms-request-id`. Needed if the client reads the
  ETag to confirm the committed blob.
- **Max age:** one hour, to cache preflights.

Rule changes propagate within a minute or two but are not instant. A failed
preflight surfaces in the browser as an opaque network error with no useful
detail in the response, so verify CORS in isolation before debugging upload
logic.

## 9. Frontend: Vue

Use `@azure/storage-blob`, which ships a browser bundle. Construct a
`BlockBlobClient` directly from the SAS URL returned by the backend — no
credential object is involved, since the SAS is embedded in the URL.

The SDK's `uploadData` method handles splitting into blocks, parallel block
upload, retries on transient failures, and the final block-list commit. Do not
hand-roll the block staging API; the failure modes around block ID encoding and
commit ordering are unpleasant.

Configure per upload:

- **Block size:** around 4 MB for typical files. Azure permits up to 4000 MiB
  per block and 50,000 blocks per blob; the practical constraint is that larger
  blocks mean more re-transmission when one fails.
- **Concurrency:** 4–8 parallel blocks. Higher saturates the connection and
  worsens the experience of anything else the user is doing.
- **Progress callback:** report bytes transferred for the UI.
- **Abort signal:** wire an `AbortController` to a cancel button and to the
  component's unmount hook, so navigating away does not leave an upload running.

The client will have one page only (not a true SPA with navigation between
pages). Therefore, upload state does not have to be stored in something like
Pinia. A DOM object is fine. Model each upload as a small
state machine: *requesting token → uploading → notifying backend → validating →
complete*, with *failed* and *cancelled* terminal states. The UI needs to
distinguish "bytes have arrived at Azure" from "the backend has accepted the
file" — these are different moments and the second can fail after the first
succeeds.

Retry policy: on a `403` from Azure mid-upload, assume SAS expiry, request a
fresh token for the same blob path, and resume. On `5xx` or network errors, the
SDK retries internally; surface a failure to the user only once its retries are
exhausted.

Distinguish this from a `401`/`403` from **our own** API, which means the
Cloudgene token has expired or the user has access to no workflows (§5).
Show a clear message and a link back to the Cloudgene login rather than
retrying. Every API call must carry the `X-Auth-Token` header, read from
`localStorage` at call time (§5.1); nothing is attached automatically, and a
`credentials:` option on `fetch` does nothing for us.

Tokens last 24 hours, so a long-lived page can outlive its token. Reading the
token per request means a renewal done in a Cloudgene tab is picked up for free;
a `401` after that is a genuine logout.

### Features

- The app should show the user what files already exist in blob under their
  prefix. Listing is a **backend** endpoint (`GET /uploads/api/files`) that
  enumerates the server-resolved prefix; do not issue the client a list-scoped
  SAS, which would be container-scoped and readable across users.
- The app should allow multiple files to be selected for upload at once.
- The app should conclude a successful upload with a list of az paths for each
  uploaded file, that should be easily copied by the user as a newline-delimited
  list.
- Don't expose per-block progress, aggregate progress in the UI.

## 10. What a SAS cannot enforce

A SAS constrains *which blob*, *which operations*, and *for how long*. It
constrains nothing about the content.

- **Size.** A token scoped to one blob for 15 minutes permits a blob of any
  size up to Azure's limits within that window. The declared size checked at
  issuance is a client-supplied claim.
- **Content type.** The client sets `x-ms-blob-content-type` to whatever it
  likes. It is a label, not a fact about the bytes.
- **Actual content.** Nothing prevents the client from uploading something
  entirely different from what it declared.

All validation is therefore post-hoc: on BlobCreated, reconcile the blob's real
properties against the pending-upload record from §7 and mark the upload failed
if they disagree. The user population here is closed and authenticated against
Cloudgene (§5), so a full quarantine-and-promote container split is not required
— but the reconciliation step is.

Mitigations at issuance are limited to blast-radius control: short expiry,
single-blob scope, create-only permission, and rate-limiting token issuance per
user so that a compromised account cannot mint thousands of write capabilities.

## 11. Observability

Log, with the upload ID as correlation key: token issuance (user, blob path,
expiry granted), client-reported completion, Event Grid arrival, validation
outcome, promotion. Also log every authentication decision — resolved user
email, group check result, and the reason for any `401`/`403` — but never the
session token itself. Logs go to the journal via systemd, as with the
downloader service.

Worth alerting on: token issuance rate per user exceeding normal, ratio of
tokens issued to uploads completed (a sudden drop suggests CORS or network
breakage), validation failure rate, and quarantine container size trending up.

The gap between issuance and Event Grid arrival is the useful latency metric —
it covers the part of the system you cannot see from the backend.

## 12. Failure modes to test explicitly

| Scenario | Expected behaviour |
|---|---|
| SAS expires mid-upload | Client renews via `POST /uploads/{id}/renew` — proactively below 1 h remaining, reactively on a 403 — and resumes staging the blocks it has not yet staged |
| Network drops mid-upload | SDK retries absorb a blip; a real outage pauses, then resumes from the in-memory staged-block set |
| User closes or reloads the tab mid-upload | Not resumable — the staged-block set lived in memory only. Partial blocks are never committed; Azure garbage-collects uncommitted blocks 7 days after the last `Put Block`, with no rule required |
| Upload completes but the client never calls back | Blob is real and billable; the expiry sweep marks the record `expired` and deletes the blob |
| Client claims completion for a blob that was never written | Validation finds no blob, marks failed |
| Client uploads 10× the declared size | Validation rejects on real properties |
| Two tokens issued for the same logical file | Distinct blob paths; both validate independently |
| Event Grid delivers twice | Idempotent validation, single promotion |
| Storage account CORS misconfigured | Preflight fails; client surfaces a clear error rather than hanging |
| No `cloudgene` key in `localStorage` (never logged in) | Client redirects to the Cloudgene login with a return URL before calling the API |
| `localStorage` value present but unparseable, or missing `token` | Treated as logged out; redirect, do not send a malformed header |
| Token present but expired or rejected | Cloudgene returns `loggedIn: false`; API responds `401`; client redirects to login |
| Valid token, but `apps` is empty (no workflow access) | `403` with an explanatory message; no SAS issued |
| Valid token, entitled user, but the account has no email set | `403` telling them to set an email in Cloudgene — not a `503`, since they can fix it themselves |
| Token forged or re-signed with the wrong key | Cloudgene rejects it; `loggedIn: false` → `401` |
| Cloudgene returns `200` with `loggedIn: false` | Treated as unauthenticated — a `200` alone must never be read as success |
| Cloudgene unreachable on loopback (restart, crash) | Fail closed with `503`; no SAS issued, nothing default-allowed |
| Cloudgene token expires between SAS issuance and completion | Bytes still upload (the SAS is independent); the completion callback returns `401` and the client re-authenticates |
| `/api/v2/server` response shape changes after a Cloudgene upgrade | Startup self-test fails loudly; no degradation to allow-all |
| Client supplies a different email, username or role in the request body | Ignored; blob prefix comes from `user.mail` in Cloudgene's response |

## 13. Stretch goals

- Support resumable uploads across browser sessions by persisting
  staged block IDs client-side.
