# Uploader API

What exists today, for an agent picking this up cold. Design rationale lives
in [`client-azure-upload.md`](client-azure-upload.md) and the task briefs
under [`tasks/`](tasks/); this page is the map, not the territory — see
[`../api/README.md`](../api/README.md) for how to run it.

## What it does

Browsers upload directly to Azure Blob Storage. This service never sees file
bytes — it authenticates the caller against Cloudgene, hands back a
short-lived create-only SAS URL for one blob, and later reconciles what
actually landed against what was declared.

## Request flow

1. `POST /uploads` — client declares `filename`, `size`, `content_type`.
   Identity comes from the `X-Auth-Token` header, forwarded to Cloudgene's
   `/api/v2/server`; never from the request body. Response carries a
   `pending` record plus `upload_url` (the blob URL with the SAS query
   string appended).
2. Client `PUT`s the file straight to `upload_url`.
3. `POST /uploads/{id}/complete` — client triggers reconciliation. The verdict
   comes from the blob's real properties, read server-side, never from
   anything the client asserts. Idempotent: a second call reports the same
   outcome, changes nothing.
4. `GET /uploads/{id}` — status at any time, owner-scoped.

A `pending` record left behind by a client that never calls back is closed
out by `store.expire_pending()`, run opportunistically on every `POST
/uploads` (no scheduler on the host).

## Modules

| File | Owns |
|---|---|
| `app.py` | Routes, `require_user` dependency, exception→HTTP mapping, startup |
| `cloudgene_auth.py` | Forwards the token to Cloudgene; interprets `loggedIn`/`apps`/`user.mail` |
| `naming.py` | `<email>/<client path>` blob paths; traversal defence (3 layers) |
| `storage.py` | SQLite (WAL) table of upload records; rate-limit queries, expiry sweep |
| `azure_sas.py` | `SasIssuer`/`BlobReader` protocols; real (cert-based) and fake issuers |
| `config.py` | Env loading; raises at startup on anything missing (fail-fast, not fail-open) |

## The trust boundary that matters

Everything hangs off one line in `cloudgene_auth.interpret_server_info`:
`is_authorised = len(apps) > 0`. `deprecatedApps`/`experimentalApps` are
deliberately excluded — entitlement to a deprecated or experimental app is
not permission to upload (operator decision, 2026-09-14; see
`test_deprecated_and_experimental_apps_do_not_authorise`). The JWT itself is
never parsed — its payload is unsigned-readable, so only Cloudgene's answer
about it counts.

Every other route decision follows from `require_user`: 401 not
authenticated, 403 no entitlement or no email on the account, 503 Cloudgene
or Azure unavailable/contract-broken. `GET`/`POST .../complete` are
owner-scoped by `store.get(upload_id, user_email)` — a leaked upload ID is
not an authorisation bypass, and a 404 covers both "doesn't exist" and
"belongs to someone else" so it can't be used to enumerate other users' IDs.

## Azure identity

Service principal, **certificate** (not secret, not managed identity —
Cloudgene runs on Nectar with no IMDS). Two role assignments at different
scopes: `Storage Blob Delegator` on the **storage account** (needed for
`generate_user_delegation_key`) and `Storage Blob Data Contributor` on the
**container**. Provisioning, rotation and revocation: [`../azure.md`](../azure.md).

SAS shape, all asserted in tests: single blob (`sr=b`, never the container),
`create` only (`sp=c`, not `w` — no overwrite), 24h expiry, start 5 minutes
in the past for clock skew, HTTPS only, no IP restriction. Delegation key
cached in-process (max lifetime 7 days; this code requests 6, refreshes an
hour before expiry), with exactly one retry on a signing failure.

`UPLOADER_SAS_ISSUER=fake` swaps in `FakeSasIssuer`, which emits
structurally identical but unsigned SAS query strings and keeps an in-memory
blob table — everything except a real Azure call is testable without
credentials.

## Not yet built

- Deleting abandoned *committed* blobs (uncommitted blocks are
  garbage-collected by Azure automatically after 7 days and need no code) —
  see `tasks/2_azure_resources.md` §2.1.
- The Vue frontend.
- Deployment itself: `../uploads.service` and `../nginx-uploads.conf` are
  written, not installed.

## Constraints that shape everything above

No SSH to the production host, by anyone, ever — verification against
`cloudgene.qcif.edu.au` is read-only `GET` requests or manual operator steps,
never a shell. No secret in the repo, the unit file, or a log line. See
`tasks/1_operator_runbook.md` for the full rule.
