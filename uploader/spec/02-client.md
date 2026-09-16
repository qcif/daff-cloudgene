# Uploader client — build plan

The Vue frontend for the service described in [`01-api.md`](01-api.md).
Design rationale is §5, §6 and §9 of
[`client-azure-upload.md`](client-azure-upload.md); this is the plan for
building it.

> ⛔ **No agent SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to
> the public host are fine. Anything needing a shell is written up for the
> operator. Nothing here deploys.

## 1. What it is

One page, served at `https://cloudgene.qcif.edu.au/uploads/`. The user picks
files, the browser uploads them straight to Azure, and the page reports what
landed. No navigation, no router, no Pinia — §9 of the design spec settles
that, and component state is sufficient.

Same-origin placement is load-bearing, not cosmetic: `localStorage` is
partitioned by origin, so this page reads Cloudgene's token only because it
shares `cloudgene.qcif.edu.au`. A separate hostname or port breaks auth
entirely.

## 2. Backend prerequisites — all shipped

The two endpoints this plan depends on exist as of Task 4
([`tasks/completed/4_list_and_renew_endpoints.md`](tasks/completed/4_list_and_renew_endpoints.md)):

- **`GET /uploads/api/files`** — the existing-files list §9 of the design
  spec requires. Reads Azure, not the record table.
- **`POST /uploads/{id}/renew`** — a fresh SAS for the *same* blob path,
  which in-session resume (§6) needs. `POST /uploads` mints a new path every
  time, so it cannot serve a resume without abandoning staged blocks.

The `az_path` field §8 depends on shipped with them. Nothing here is
blocked; the build brief is
[`tasks/5_build_client.md`](tasks/5_build_client.md).

One later addition is **not** shipped: `DELETE /uploads/api/files`, and the
`client_path` field on the list response that §8.1 sends back to it. Both
are specified in [`tasks/08-delete-files.md`](tasks/08-delete-files.md) and
must land before the delete button does.

## 3. The trap that will silently fail every upload

`app.reconcile_upload()` compares the blob's real content type against the
declared one and marks the upload **failed** on a mismatch. If the client
does not set the content type, Azure defaults a block blob to
`application/octet-stream` — so declaring `text/csv` and not setting the
header means every CSV upload transfers perfectly and is then marked failed.

The client **must** pass the same value it declared:

```js
await blockBlobClient.commitBlockList(blockIds, {
  blobHTTPHeaders: { blobContentType: declaredContentType },
});
```

Derive `declaredContentType` once, use it for both the `POST /uploads` body
and the upload, and never let the two drift. Same for size: declare
`file.size` exactly.

## 4. Module layout

`uploader/client/`, built with Vite, `base: '/uploads/'` (§6 — without it
every asset URL resolves against `/` and 404s through the Cloudgene proxy).

| File | Owns |
|---|---|
| `src/auth.js` | Read the token from `localStorage`; redirect to login |
| `src/api.js` | `fetch` wrapper for our API; attaches `X-Auth-Token`; maps error codes |
| `src/upload.js` | One file's state machine; wraps `@azure/storage-blob` |
| `src/validate.js` | Client-side mirror of the server's filename/extension rules |
| `src/App.vue` | The page: file picker, existing files, progress, results |
| `src/components/` | `FileRow.vue`, `ExistingFiles.vue`, `ResultList.vue` |

`@azure/storage-blob` is constructed from the SAS URL alone —
`new BlockBlobClient(sasUrl)`. No credential object, and no Azure secret ever
reaches the browser.

## 5. Auth handling

Read `localStorage['cloudgene']` → `JSON.parse` → `.token`, **per request**,
never cached in a module variable. A token renewed in another Cloudgene tab
is then picked up for free; a stale copy would produce a spurious logout.

| Situation | Client behaviour |
|---|---|
| Key absent, unparseable, or no `token` | Redirect to Cloudgene login with a return URL, before calling the API |
| `401` from our API | Same — the token expired or was rejected |
| `403` from our API | Show the message verbatim. It is actionable: no workflow access, or no email set on the account |
| `503` from our API | "Try again shortly." Not a login problem; do not redirect |

Never send a malformed header. Never `fetch(..., {credentials})` — the
credential is a custom header, which is also what makes this API
CSRF-resistant. Do not migrate it to cookies.

## 6. Upload state machine

Per file: `queued → requesting → uploading → completing → complete`, with
`failed` and `cancelled` as terminal states.

The UI must distinguish **"bytes reached Azure"** from **"the backend
accepted it"**. They are different moments, and the second can fail after the
first succeeds — that is exactly what reconciliation is for.

- Block size ~4 MB, concurrency 4–8.
- Aggregate progress only. §9 is explicit: no per-block display.
- One `AbortController` per file, wired to a cancel button **and** to
  `onBeforeUnmount`, so navigating away does not leave transfers running.
- On completion, `POST /uploads/{id}/complete`, then render that response's
  `state`. It is idempotent, so a retry button can simply call it again.

### 6.1 In-session resume

Resume within the page session is **in scope** for network interruptions and
SAS expiry (operator, 2026-09-15). Across sessions it is not — a reload
starts over.

This is the one place the design spec's "do not hand-roll the block staging
API" guidance (§9) has to give way. `uploadData()` cannot resume: it
generates fresh block IDs per call, so a retry re-uploads everything. Stage
blocks explicitly instead:

- Split the file into fixed 4 MB blocks with **deterministic** block IDs —
  `btoa("block-" + String(i).padStart(6, "0"))`, equal length for every
  block, as Azure requires.
- Keep the set of successfully staged indices in component memory.
- On resume, stage only the missing indices, then
  `commitBlockList(allIds, { blobHTTPHeaders: { blobContentType } })`.
  The content type must be set **here as well as** on the blocks — see §3.

Uncommitted blocks survive on Azure for seven days, so the staged work is
still there when the network returns.

**Why it cannot survive a reload:** recovering the staged list from Azure
needs `getBlockList('uncommitted')`, which requires `r` on the SAS. Ours is
create-only, and that is worth more than the feature. The in-memory list is
the only record, so it dies with the page. This is a deliberate boundary, not
an oversight.

### 6.2 What triggers a resume

| Trigger | Response |
|---|---|
| Transient network error | The SDK's own retries handle it; configure generous `retryOptions` and do nothing else |
| Retries exhausted (a real outage) | Pause, surface "connection lost — retrying", resume staging when a manual retry or reconnect fires |
| SAS nearing expiry (<1 h left) | Renew **proactively** via `POST /uploads/{id}/renew`, before anything fails |
| `403` from Azure mid-upload | Renew reactively, rebuild the `BlockBlobClient` from the new URL, continue |
| Renewal returns `404`/`409` | The record is terminal — the upload cannot be resumed. Start a fresh one |

Renew proactively, not just on `403`. A record whose `expires_at` lapses gets
swept to `expired` by the backend and is then unrenewable, so waiting for the
failure can strand an upload that was minutes from finishing.

## 7. Client-side validation

Mirror `naming.py` and the `config.py` allowlists, to fail fast with a clear
message rather than burning a round trip. The server check remains
authoritative — this is courtesy, not a control.

Reject before requesting a token: empty name, a disallowed extension,
`size` of 0, and anything `naming.validate_client_path` would refuse
(backslash, colon, `%`, control characters, trailing dot, leading/trailing
whitespace). Use `file.name` only — not `webkitRelativePath` — so the client
proposes a single leaf segment and the server owns the whole prefix.

Set `accept` on the file input from the allowlist, and permit multiple
selection (§9).

## 8. Result output

On success, render a copyable newline-delimited list of the uploaded paths
(§9), with one copy-to-clipboard button for the whole block.

The form is `az://container/path/to/file.txt` — the storage account is
inferred from Nextflow context, so it does not appear:

```
az://uploads/user@example.com/reads_R1.fastq.gz
az://uploads/user@example.com/reads_R2.fastq.gz
```

Render the `az_path` field the API returns. Do **not** build it client-side
from a hardcoded container name — that is one deployment away from emitting
paths that point at the wrong container.

The same applies to the existing-files table, which is the *other* place a
user copies a path from: it shows the full `az://` path, monospaced, with a
per-row copy button and a `title` attribute so a truncated cell is still
readable. The blob path alone is not a useful value to show — nobody pastes
it anywhere. That table currently renders `blob_path`;
[`tasks/09-az-path-in-file-list.md`](tasks/09-az-path-in-file-list.md) is
the change.

## 8.1 Deleting a file

Each row of "Your files in storage" carries a delete button, behind a
`confirm()` naming the full `az://` path and stating that it cannot be
undone. The request is `DELETE /uploads/api/files` with the row's
`client_path` — the leaf within the user's own prefix, taken from the list
response, never derived by slicing the blob path. The server rebuilds the
full path from the resolved identity; see
[`tasks/08-delete-files.md`](tasks/08-delete-files.md) §2.

It is idempotent, so `deleted: false` ("it was already gone") and
`deleted: true` take the same UI path: drop the row, then reload the list so
the table reflects Azure rather than a guess. A `409` means an upload to
that path is still in progress — show the message verbatim against that row
and leave the row alone. Errors are per row: one failed delete must not
clear the table or the upload results.

`ExistingFiles.vue` emits rather than calling the API, so `App.vue` stays
the only module that talks to `api.js`.

## 9. Local development

The dev server is a different origin from Cloudgene, so **there is no token
in `localStorage`** and §5 cannot work as deployed. Provide a dev-only
escape hatch: read `import.meta.env.VITE_DEV_TOKEN` when
`import.meta.env.DEV` is set, and fall back to `localStorage`. It must be
impossible for that branch to survive a production build.

Proxy the API in `vite.config.js` so paths match production:

```js
server: { proxy: { '/uploads/api': { target: 'http://127.0.0.1:8003',
                                     rewrite: p => p.replace(/^\/uploads\/api/, '') } } }
```

Run the backend with `UPLOADER_SAS_ISSUER=fake` (see
[`../api/README.md`](../api/README.md)) to exercise everything except the
real Azure transfer. The fake issuer's SAS URLs are structurally correct but
Azure will reject them, so the `uploading` state cannot be tested against
real storage without credentials — test the state machine against the fake
and the transfer manually once the container is reachable.

## 10. Decision needed: how `dist/` reaches the server

This is the first build-tooled frontend in the repo — the downloader is a
single `index.html` with no build step — and
[`../deploy/nginx-uploads.conf`](../deploy/nginx-uploads.conf) aliases `/uploads/` straight
at `uploader/client/dist/` inside the deployed checkout. So either:

- **Commit `dist/`** (recommended). Deployment stays `git pull`, and the
  production host needs no Node toolchain. The cost is build output in
  version control and noisy diffs. Needs a `!uploader/client/dist` exception,
  since `.gitignore` does not currently cover it either way.
- **Build on the server.** Clean history, but Node and `npm ci` become a
  production dependency, and the operator gains a build step in a deployment
  that is currently just a pull.

Settled on the first; [`tasks/5_build_client.md`](tasks/5_build_client.md)
§11 carries it, including the `.gitignore` change. `node_modules/` is
ignored either way.

## 11. Tests

Vitest, on the parts worth testing: `validate.js` against the same cases as
`test_naming.py`, the token read in `auth.js` (absent, unparseable, no
`token` key), the error mapping in `api.js`, and the state machine's
transitions with a stubbed uploader. No E2E, and no test touches the network
or Azure.

Resume deserves real coverage, since it is the part that only runs when
something has already gone wrong: block IDs are deterministic and equal
length; a resume re-stages only the missing indices; a proactive renewal
fires before expiry rather than after; and a terminal record surfaces as
"start again" rather than an infinite retry.

## 12. Out of scope

Resume across browser sessions — a reload starts over, for the reason in
§6.1. Per-block progress, renaming of existing blobs, bulk or multi-select
delete, and folder uploads.

Single-file deletion **was** out of scope and is now in — see §8.1 and
[`tasks/08-delete-files.md`](tasks/08-delete-files.md).
