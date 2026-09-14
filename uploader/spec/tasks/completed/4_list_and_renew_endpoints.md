# Task 4 — listing, SAS renewal, and `az://` paths

Three additions to the FastAPI backend in [`../../api/`](../../api/). All
three block the client build ([`../02-client.md`](../02-client.md)); none of
them is speculative groundwork.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Anything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server.

Read [`3_build_fastapi_backend.md`](3_build_fastapi_backend.md) first. Its
settled decisions all still hold — in particular the authorisation rule
(`loggedIn` and a non-empty `apps`; `deprecatedApps`/`experimentalApps` do
**not** count), fail-closed error handling, and owner-scoping every read.

## 1. `GET /files` — list the user's own prefix

§9 of the design spec requires the page to show what the user already has in
storage. Nothing implements it: there is no route, and no blob-listing call
into Azure anywhere. `storage.list_for_owner()` exists but nothing routes to
it.

**List Azure, not the SQLite table.** The records say what was *promised*;
the container says what is *there*. They diverge whenever a blob is deleted
out of band, or a client uploads its bytes and never calls back. Take the
Azure listing as the source of truth and annotate it with record state where
a blob path matches.

### Implementation

- Add `list_blobs(prefix)` to `azure_sas.py`, backed by
  `ContainerClient.list_blobs(name_starts_with=...)`. Container-scoped
  `Storage Blob Data Contributor` already permits list — no new role
  assignment, nothing for the operator to do in Azure.
- Give it its own protocol (`BlobLister`) alongside `SasIssuer` and
  `BlobReader`, for the reason the other two are separate: a future
  reconciliation worker needs listing without needing to sign.
- Implement it on `FakeSasIssuer` too, over its in-memory blob table, so the
  route is testable without credentials.
- Route it behind `require_user`, exactly like the others.

### The trailing slash is a security control

The prefix is `naming.prefix_for(email)`, which returns `<email>/`. **That
trailing slash is what stops one user's listing from including another's.**
Without it, `bob@example.com` would match `bob@example.com.au/...`. It looks
like a cosmetic detail and it is not; add a test that names two such users
explicitly, so the invariant survives someone tidying the code later.

Nothing from the request may influence the prefix. No `prefix`, `path` or
`user` query parameter — not even an optional one that defaults correctly.

### Response

Per blob: the blob path, `az_path` (§3), size, content type, last-modified.
Where a `completed` upload record matches the blob path, include its
`upload_id` and `state`.

Cap the result at a configurable maximum (`UPLOADER_MAX_LIST_RESULTS`,
default 1000) and return a `truncated` boolean rather than paginating. If a
user ever hits that ceiling, pagination is the smaller of their problems.

## 2. `POST /uploads/{id}/renew` — a fresh SAS for the same blob

Resumability across browser sessions stays out of scope. Resumability
**within** a session — surviving a network interruption or a SAS that
expires mid-transfer — is now in scope (operator decision, 2026-09-15), and
the client cannot do it without a way to re-sign the same blob path.

### Why the existing route will not serve

`POST /uploads` mints a **new** blob path and a **new** record every time. A
client that used it to recover would abandon its staged blocks and restart
from zero, which is the behaviour we are removing.

### Behaviour

Owner-scoped via `store.get(upload_id, email)`, so a leaked upload ID is not
a bypass and a miss is a 404 covering both "no such upload" and "not yours".

- **Only while `state = 'pending'`.** A terminal record is terminal — do not
  revive one. `expired` means start a new upload.
- Re-signs the **same** `blob_path` with the same parameters as issuance:
  single blob, create-only, HTTPS, 24h.
- **Extends the record's `expires_at`.** This is not optional.
  `expire_pending()` sweeps on `expires_at < now`, so a renewal that does not
  push the record's expiry forward leaves an upload that is actively
  transferring to be marked `expired` underneath itself.
- Bounded by a per-record renewal cap (`UPLOADER_MAX_RENEWALS`, default 10)
  rather than by the global rate limiter. A renewal creates no row, so
  `count_issued_since()` cannot see it; a per-record cap bounds abuse without
  a second rate-limit query. Requires a `renewal_count` column — add it to
  `SCHEMA` with a migration-free default, since the table may already exist.
- Entitlement is re-checked live, because the route sits behind
  `require_user`. A user who loses access mid-upload cannot renew. That is
  consistent with the completion callback, and it is the same open question
  flagged in Task 3 — do not resolve it here, in either direction.

### Create-only permission is sufficient — but verify it

`Put Block` and `Put Block List` are permitted by SAS `c` (Create) while the
blob does not yet exist, and uncommitted blocks belong to the *blob name*,
not to the SAS that staged them. A second create-only SAS should therefore
be able to commit blocks staged under the first.

**This has never been executed against real Azure.** No code in this repo has
made a real Azure call. Treat it as the assumption the design rests on, and
have the operator confirm it with a two-block manual test before the client
work depends on it. If it turns out `w` is required, escalate rather than
quietly widening the permission — dropping create-only would allow
overwriting any blob the SAS names.

## 3. `az://` paths in responses

Workflow input fields want `az://container/path/to/file.txt`, with the
storage account inferred from Nextflow context (operator, 2026-09-15).

Add `Config.az_path(blob_path)` returning `f"az://{container}/{blob_path}"`,
alongside the existing `blob_url()`. Include an `az_path` field in the
`POST /uploads` response, `GET /uploads/{id}`, and every entry from
`GET /files`.

Build it server-side, from config. The client must not concatenate a
container name it has hardcoded — that is one deployment away from emitting
paths that point at the wrong container.

`serialise()` currently has no access to config; pass the container in
rather than reaching for a module-level global.

## 4. Tests

`unittest`, not pytest. No test may touch the network or need credentials —
`FakeSasIssuer` covers all three features.

Cover at least:

- Listing returns only the caller's prefix, with the
  `bob@example.com` / `bob@example.com.au` case named explicitly.
- Listing reflects a blob that exists with no matching record, and a record
  whose blob is gone.
- No request parameter can widen or move the prefix.
- Renewal returns a SAS for the *same* blob path.
- Renewal extends `expires_at`, and the renewed record survives a subsequent
  `expire_pending()` that would otherwise have swept it.
- Renewal of a `completed`, `failed` or `expired` record is refused.
- Renewal of another user's upload is a 404, not a 403.
- The renewal cap is enforced.
- `az_path` is `az://<container>/<blob path>` and comes from config.

Then run the full suite and flake8 (`/home/cameron/.local/envs/claude/bin/flake8`).

## 5. Out of scope

- Deleting abandoned committed blobs. Still specified in
  [`2_azure_resources.md`](2_azure_resources.md) §2.1 and still
  unimplemented — a separate task. Uncommitted blocks need no action; Azure
  garbage-collects them seven days after the last `Put Block`.
- Cross-session resume. The client tracks staged block IDs in memory only,
  and deliberately cannot recover them after a reload: reading the staged
  block list needs `r` on the SAS, and create-only is a property worth more
  than the feature.
- Deletion or renaming of existing blobs through this API.
- Any change to the authorisation rule.
