# Task 5 — build the uploader client

Build the Vue frontend at `uploader/client/`, served at
`https://cloudgene.qcif.edu.au/uploads/`. The backend it talks to is
finished: every endpoint this task needs exists and is tested.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Anything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server.

## 0. Read these first

| Document | Why |
|---|---|
| [`../02-client.md`](../02-client.md) | The build plan. This task is its execution; that document holds the reasoning, this one the checklist |
| [`../01-api.md`](../01-api.md) | What the backend is and where its trust boundary sits |
| [`../../api/README.md`](../../api/README.md) | Route table, env vars, how to run it locally |
| [`../client-azure-upload.md`](../client-azure-upload.md) §5, §6, §9, §12 | Design rationale — auth, deployment, frontend, failure modes |
| [`completed/4_list_and_renew_endpoints.md`](completed/4_list_and_renew_endpoints.md) | The endpoints added for this client, and why they behave as they do |

Nothing is blocked. `GET /files`, `POST /uploads/{id}/renew` and the
`az_path` field all shipped in Task 4.

## 1. Deliverable

One page. File picker, a list of what the user already has in storage,
per-file progress, and a copyable list of `az://` paths at the end. No
router, no Pinia — §9 of the design spec settles that, and component state
is sufficient.

Match the existing look: Cloudgene and
[`../../../downloader/index.html`](../../../downloader/index.html) both use
Bootstrap 4.6 and Font Awesome 5. Use the same, installed via npm rather
than a CDN — the page must keep working if a CDN is blocked, and the assets
are already being built.

## 2. Project setup

```
uploader/client/
├── index.html
├── package.json
├── vite.config.js
├── src/
│   ├── main.js
│   ├── App.vue
│   ├── api.js
│   ├── auth.js
│   ├── upload.js
│   ├── validate.js
│   └── components/{FileRow,ExistingFiles,ResultList}.vue
└── tests/
```

Vue 3 + Vite, `@azure/storage-blob`, Vitest. **`base: '/uploads/'` in
`vite.config.js`** — without it every asset URL resolves against `/` and
404s through the Cloudgene proxy, which is the single most likely way this
deploys broken.

## 3. The API, as actually built

Base URL `/uploads/api`. Every route below needs the `X-Auth-Token` header.

| Route | Returns |
|---|---|
| `POST /uploads` | `201` + the record, plus `upload_url` (blob URL with SAS) |
| `POST /uploads/{id}/renew` | The record with a pushed-out `expires_at`, plus a fresh `upload_url`. No body |
| `POST /uploads/{id}/complete` | The record plus `changed`. Idempotent. No body |
| `GET /uploads/{id}` | The record |
| `GET /files` | `{"files": [...], "truncated": bool}`. No parameters |

The record shape, from `app.serialise()`:

```json
{
  "upload_id": "...", "blob_path": "user@example.com/reads.fastq.gz",
  "az_path": "az://uploads/user@example.com/reads.fastq.gz",
  "declared_size": 12345, "declared_content_type": "application/gzip",
  "issued_at": "...", "expires_at": "...",
  "state": "pending", "detail": null
}
```

`state` is one of `pending`, `completed`, `failed`, `expired`. `detail`
carries the reason on `failed` and is worth showing verbatim — it names the
actual mismatch.

A `GET /files` entry is `blob_path`, `az_path`, `size`, `content_type`,
`last_modified`, plus `upload_id` and `state` when a record matches. Blobs
with no matching record are normal, not an error: bytes landed and the
client never called back. Render them, without a state badge.

`POST /uploads` takes exactly `{filename, size, content_type}`. Any other
field is ignored — do not bother sending an email or username, the server
resolves identity from the token and nothing else.

### Error codes

| Code | Meaning | Client behaviour |
|---|---|---|
| `400` | A declaration or path check failed | Show `detail`; it names the failing check |
| `401` | Not signed in to Cloudgene | Redirect to login with a return URL |
| `403` | No workflow access, or no email on the account | Show `detail` **verbatim** — it is actionable and tells the user what to fix |
| `404` | No such upload, or not yours | Terminal. Start over |
| `409` | Renewal on a non-pending record | Terminal. Start a fresh upload |
| `429` | Rate limited, or the renewal cap is spent | Honour `Retry-After` when present |
| `503` | Cloudgene or Azure unavailable | "Try again shortly." **Not** a login problem — do not redirect |

Every error body is `{"detail": "..."}`. One shape, always.

## 4. Auth

Read `localStorage['cloudgene']` → `JSON.parse` → `.token`, **per request**,
never cached in a module variable. A token renewed in another Cloudgene tab
is then picked up for free; a cached copy produces a spurious logout.

Absent, unparseable, or no `token` key → redirect to the Cloudgene login
with a return URL, before calling the API at all.

Never parse the JWT. Its payload is unsigned-readable, so anything read from
it is a claim, not a fact — and its `sub` is the username, not the email the
prefix is built from. Only the server's answer counts.

Never use `fetch(..., {credentials})`. The credential is a custom header,
which is also what makes this API CSRF-resistant. Do not migrate it to
cookies.

Same-origin placement is load-bearing: `localStorage` is partitioned by
origin, so this page can read Cloudgene's token only because it shares
`cloudgene.qcif.edu.au`. A separate hostname or port breaks auth entirely.

## 5. The trap that will silently fail every upload

`app.reconcile_upload()` compares the blob's **real** content type against
the declared one and marks the upload `failed` on a mismatch. Azure defaults
a block blob with no content type set to `application/octet-stream` — so
declaring `text/csv` and not setting the header means the file transfers
perfectly and is then marked failed.

Derive the content type once, and pass that same value to both the
`POST /uploads` body and the commit:

```js
commitBlockList(blockIds, {
  blobHTTPHeaders: { blobContentType: declaredContentType },
});
```

Same for size: declare `file.size` exactly. The server compares byte counts.

`file.type` is empty for most of the allowed extensions (`.fastq`, `.ab1`,
`.fq`). Map extension → content type yourself and fall back to
`application/octet-stream`, which is on the allowlist.

## 6. Client-side validation

Mirror [`../../api/naming.py`](../../api/naming.py) and the `config.py`
allowlists, to fail fast rather than burn a round trip. The server check
stays authoritative — this is courtesy, not a control, and it must never be
the only check.

Reject before requesting a token: empty name, `size` of 0, a disallowed
extension, and anything `naming.validate_client_path()` refuses — backslash,
colon, `%`, control characters, a trailing dot on any segment, leading or
trailing whitespace, a leading `/`, more than 8 segments, a segment over 255
characters.

Use `file.name` only, never `webkitRelativePath`, so the client proposes a
single leaf segment and the server owns the whole prefix.

Set `accept` on the file input from the allowlist, and permit multiple
selection. The allowlist is server config (`UPLOADER_ALLOWED_EXTENSIONS`), so
hardcoding it in the client means two lists that can drift — acceptable for
now, but put both the extension and content-type maps in one module with a
comment naming `config.py` as the source of truth.

## 7. Upload engine

Per file: `queued → requesting → uploading → completing → complete`, with
`failed` and `cancelled` terminal.

The UI must distinguish **"bytes reached Azure"** from **"the backend
accepted it"**. They are different moments and the second can fail after the
first succeeds — that is what reconciliation is for.

- 4 MB blocks, concurrency 4–8.
- Aggregate progress only. §9 is explicit: no per-block display.
- One `AbortController` per file, wired to a cancel button **and** to
  `onBeforeUnmount`.
- On transfer completion, `POST /uploads/{id}/complete`, then render that
  response's `state`. It is idempotent, so a retry button can simply call it
  again.

### 7.1 Stage blocks explicitly

This is the one place the design spec's "do not hand-roll the block staging
API" guidance (§9) is overridden, and the reason is resumability:
`uploadData()` generates fresh block IDs on every call, so a retry
re-uploads the whole file.

- Fixed 4 MB blocks with **deterministic, equal-length** block IDs —
  `btoa("block-" + String(i).padStart(6, "0"))`. Azure requires every block
  ID in a blob to be the same length; a variable-length scheme fails at
  commit time, on large files only.
- `stageBlock(id, chunk, size)` per block, bounded concurrency.
- Track successfully staged indices in component memory.
- On resume, stage only the missing indices, then `commitBlockList(allIds,
  { blobHTTPHeaders: { blobContentType } })`.

Uncommitted blocks survive on Azure for seven days, so staged work is still
there when the network returns. A blob does not exist until
`commitBlockList` — a cancelled or abandoned upload leaves nothing visible.

**This was verified against real Azure on 2026-09-15.** Two create-only SAS
tokens were issued for the same blob path, a block staged under each, and
both committed using only the second token's authority — see
[`completed/4_operator_runbook.md`](completed/4_operator_runbook.md). Renewal
mid-upload works; you are not building on an assumption.

### 7.2 Resume: in-session only

| Trigger | Response |
|---|---|
| Transient network error | The SDK retries internally. Configure generous `retryOptions` and do nothing else |
| Retries exhausted (a real outage) | Pause, surface "connection lost", resume staging on manual retry or reconnect |
| SAS under 1 h from `expires_at` | Renew **proactively** via `POST /uploads/{id}/renew` |
| `403` from Azure mid-upload | Renew reactively, rebuild the `BlockBlobClient` from the new `upload_url`, continue |
| Renew returns `404`/`409` | Terminal. Surface "start again", never an infinite retry |

Renew proactively, not only on `403`. A record whose `expires_at` lapses is
swept to `expired` by the backend's opportunistic sweep and is then
unrenewable — waiting for the failure can strand an upload that was minutes
from finishing. Renewal is capped per record (`UPLOADER_MAX_RENEWALS`,
default 10), so renew on a schedule, not in a loop.

Across sessions, resume is **out of scope**: a reload starts over.
Recovering the staged block list from Azure needs
`getBlockList('uncommitted')`, which requires `r` on the SAS, and ours is
create-only. That permission is worth more than the feature. The in-memory
list is the only record and it dies with the page — a deliberate boundary,
not an oversight. Do not widen the SAS to work around it; escalate instead.

## 8. Results

On success, a copyable newline-delimited block of `az://` paths, with one
copy-to-clipboard button for the whole thing:

```
az://uploads/user@example.com/reads_R1.fastq.gz
az://uploads/user@example.com/reads_R2.fastq.gz
```

Render the `az_path` the API returns. Do **not** build it client-side from a
hardcoded container name — that is one deployment away from emitting paths
that point at the wrong container. The storage account is absent by design;
Nextflow infers it from context.

## 9. Local development

The dev server is a different origin from Cloudgene, so **there is no token
in `localStorage`** and §4 cannot work as deployed. Provide a dev-only
escape hatch: read `import.meta.env.VITE_DEV_TOKEN` when
`import.meta.env.DEV` is set, falling back to `localStorage`. It must be
impossible for that branch to survive a production build — assert that in a
test, and check the built bundle for the string once.

Proxy the API so paths match production:

```js
server: { proxy: { '/uploads/api': { target: 'http://127.0.0.1:8003',
                                     rewrite: p => p.replace(/^\/uploads\/api/, '') } } }
```

Run the backend with `UPLOADER_SAS_ISSUER=fake` (see
[`../../api/README.md`](../../api/README.md)). The fake issuer's SAS URLs are
structurally correct but Azure rejects them, so the `uploading` state cannot
be exercised against real storage without credentials. Test the state
machine against the fake; the real transfer is a manual check once the
container is reachable.

Obtaining a real dev token: sign in to `cloudgene.qcif.edu.au` and copy
`localStorage['cloudgene']`. Put it in `.env.local`, which is gitignored
(`.env.*`). Never commit it, never log it, never put it in a test fixture.

## 10. Tests

Vitest. No test touches the network or Azure.

- `validate.js` against the same cases as `test_naming.py` — including the
  ones that must be *rejected*: `%`, backslash, colon, trailing dot,
  `../`, absolute paths.
- `auth.js`: key absent, unparseable JSON, present but no `token` key.
- `api.js`: each status code maps to the right client behaviour, and the
  header is attached on every call.
- The state machine, with a stubbed uploader.

Resume deserves real coverage, since it only runs when something has already
gone wrong:

- Block IDs are deterministic and **equal length**, including at indices
  that change digit count (block 9 → 10, 999 → 1000).
- A resume re-stages only the missing indices.
- A proactive renewal fires before expiry, not after.
- A `409` surfaces as "start again" rather than a retry loop.
- The content type is set on `commitBlockList`, not only on the record.

## 11. Build and deployment

[`../../nginx-uploads.conf`](../../nginx-uploads.conf) aliases `/uploads/`
directly at `uploader/client/dist/` in the deployed checkout, so **commit
`dist/`**. Deployment stays `git pull`; the production host needs no Node
toolchain. The cost is build output in version control and noisy diffs, and
that is the accepted trade.

`.gitignore` currently covers neither `dist/` nor `node_modules/`. Add:

```
node_modules/
!uploader/client/dist/
```

Do not deploy. The nginx snippet and the systemd unit are both written and
neither has been applied; installing them is the operator's, not this
task's.

## 12. Out of scope

Resume across browser sessions (§7.2). Per-block progress. Deleting or
renaming existing blobs. Folder uploads. Pagination of `GET /files` — it
caps and flags `truncated`, and showing that flag is enough. Any change to
the backend's authorisation rule.

## 13. Done when

- A file selected in the browser lands in Azure and `GET /files` shows it.
- Killing the network mid-upload and restoring it resumes from the staged
  blocks rather than byte zero.
- An expired SAS renews without the user noticing.
- A CSV uploads and reconciles to `completed`, not `failed` — the content
  type check in §5 is the one that catches a plausible-looking build that is
  broken for every file.
- `npm run test` passes; `npm run build` emits into `dist/` with
  `/uploads/`-prefixed asset URLs.
