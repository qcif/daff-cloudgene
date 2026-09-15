# Task 6 — a local filesystem stand-in for Azure Blob Storage

Close the last gap in local development: make the byte transfer work
offline, so the whole UX flow — pick a file, watch it upload, see it
reconciled, see it listed — can be exercised with no Azure credentials and
no network.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Anything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server.

Read [`3_build_fastapi_backend.md`](completed/3_build_fastapi_backend.md)
and [`4_list_and_renew_endpoints.md`](completed/4_list_and_renew_endpoints.md)
first. Their settled decisions all still hold.

## 0. What this is and is not

Two fakes already exist. `UPLOADER_SAS_ISSUER=fake` hands out structurally
correct but unsigned SAS URLs; `UPLOADER_AUTH_PROVIDER=fake` answers auth
from recorded fixtures. Together they bring the page to life as far as
Azure, and then stop: `FakeSasIssuer` builds its URLs from the real blob
endpoint, so the browser stages blocks against
`daffstandard.blob.core.windows.net` with a signature Azure rejects, gets a
`403`, and progress, reconciliation and the `az://` list stay unreachable.

This task adds the third and last fake: a **local filesystem blob store**
that the browser can really upload to.

**The purpose is seeing the UX flow work end to end, not proving Azure
behaves.** Anything requiring real Azure semantics is verified on the
production server against real auth and real storage (operator decision,
2026-09-15). Do not grow this into an Azure emulator.

### The wire surface is two requests

This was measured against the SDK the client actually bundles, not inferred
from documentation. `BlockBlobClient` pointed at a plain `http://127.0.0.1`
host issues exactly:

```
PUT /{container}/{blob}?{sas}&comp=block&blockid=YmxvY2stMDAwMDAw
    content-type: application/octet-stream
    x-ms-version, x-ms-client-request-id
    body: the raw block bytes

PUT /{container}/{blob}?{sas}&comp=blocklist
    content-type: application/xml
    x-ms-version, x-ms-blob-content-type, x-ms-client-request-id
    body: <?xml …?><BlockList><Latest>YmxvY2stMDAwMDAw</Latest>…</BlockList>
```

No `GET`, no `HEAD`, no `Authorization` header. The SDK appends `comp=` and
`blockid=` to whatever query string the URL already carries, so an existing
SAS query string survives untouched. That is the whole protocol to
implement.

## 1. Shape: a separate process, not a router on the API

Run the blob stand-in as its own ASGI app in its own process, on its own
port (`127.0.0.1:8004` by default). **Not** a conditionally-mounted router
inside `app.py`.

Three reasons, in order of importance:

1. **It structurally cannot ship.** A router guarded by
   `if config.issuer == "local"` is one refactor away from being reachable
   in production. A module the production app never imports is not.
2. **The path shape stays honest.** Azure's is `/{container}/{blob}`. The
   production app already owns `/uploads`, which collides with the container
   name — so mounting inside it would force a prefix like `/_dev/blob/…`,
   and `BlockBlobClient` would then parse `containerName` and `name` wrongly
   off the URL. Nothing in `upload.js` reads those properties today, but
   there is no reason to plant the trap.
3. **CORS gets exercised.** A separate origin means the browser really does
   preflight, which is the point of §4.

This also mirrors production honestly: two local processes plus Azure
becomes two local processes plus a third that stands in for it.

Suggested layout — a new top-level module, not under `api/`:

```
uploader/devblob/
    server.py       the two PUT routes, CORS, Azure-shaped errors
    store.py        block staging and commit over a directory
    README.md       one paragraph: what it is, why it is not in api/
```

`uploader/api/**` must not import it. Add a test that asserts so.

## 2. `LocalFileSasIssuer` in the backend

A third issuer, `UPLOADER_SAS_ISSUER=local`, implementing all three existing
protocols (`SasIssuer`, `BlobReader`, `BlobLister`) over a directory. This is
`FakeSasIssuer` with its in-memory `self._blobs` dict replaced by the
filesystem, so records survive a restart and the API reads the same bytes
the browser wrote.

| Method | Behaviour |
|---|---|
| `issue()` | Same SAS parameter shape as `FakeSasIssuer` (`sr=b`, `sp=c`, `st`, `se`, `sig`), but `blob_url` points at the local endpoint |
| `get_blob_properties()` | `stat()` the file; content type from the sidecar (§3) |
| `list_blobs()` | Walk the root, returning paths relative to it |

Register it in `VALID_ISSUERS` and `create_issuer()`, and log the same
loud warning `FakeSasIssuer` logs. It must never be the default — a host
that forgets the variable still gets the real Azure issuer.

Two new settings, **honoured only when the issuer is `local`**, and ignored
entirely otherwise:

| Variable | Default | Purpose |
|---|---|---|
| `UPLOADER_LOCAL_BLOB_ROOT` | `/tmp/uploader-blobs` | Where blobs land |
| `UPLOADER_LOCAL_BLOB_ENDPOINT` | `http://127.0.0.1:8004` | Host in issued URLs |

**Do not make `BLOB_ENDPOINT_TEMPLATE` configurable.** The comment at
[`config.py:105`](../../api/config.py#L105) explains why the account name is
the only variable part, and that reasoning is unchanged: a misconfiguration
must not be able to point production SAS URLs at an arbitrary host. The
local endpoint is a separate setting that the `azure` issuer never reads.

`az_path` keeps returning `az://{container}/{blob_path}` from the real
configured container. The point is to exercise the display logic that
production uses, not to show a local directory.

## 3. The store

Layout under the root:

```
{root}/{email}/{client path}          the committed blob
{root}/{email}/{client path}.meta     JSON sidecar: content_type
{root}/.staged/{blob path}/{block id} uncommitted blocks
```

A filesystem has no content type, so the sidecar carries what
`x-ms-blob-content-type` set at commit. Reconciliation compares the declared
content type against what actually landed, and without the sidecar that
check would be untestable locally.

Keep staged blocks outside the visible tree (`.staged/`) and exclude that
directory from `list_blobs()`. Azure does not list a blob until its blocks
are committed, and the `GET /files` view must not show half-finished
uploads.

**Commit is not "concatenate what arrived".** Order comes from the
`<BlockList>` XML, not from arrival order or filename sort — the client
stages blocks with six-way concurrency, so they land out of order routinely.
Getting this wrong produces a corrupt file that still looks plausible.

Behaviours worth reproducing, each about one line:

- **Create-only.** If the target blob already exists at commit, return
  `409` with `BlobAlreadyExists`. `sp=c` rather than `sp=w` is the security
  property the whole design rests on; a stand-in that silently overwrites
  trains the wrong expectation.
- **Expiry.** Parse `se=` from the query string and return `403` once it has
  passed. This is what makes the renewal flow reachable — re-sign, stage
  under SAS B, commit blocks staged under SAS A. That flow is the design's
  least obvious assumption, and it is otherwise untestable without real
  credentials. Blocks belong to the blob path, not to the SAS that staged
  them, so commit must not care which SAS staged what.
- **Ignore `sig` entirely.** There is nothing to verify and nothing gained
  by pretending.

**Validate the blob path from the URL.** Reuse the traversal defences in
`naming.py` rather than writing new ones, and reject anything that does not
survive them with a `400`. This is a dev tool, but it is a dev tool that
writes attacker-controlled paths to disk.

**Fail loudly on anything unrecognised.** Any request that is not
`comp=block` or `comp=blocklist` gets a `400`, never a cheerful `201`. The
real risk of a hand-rolled stand-in is silent divergence: a client bug that
passes locally and fails in production. Loud rejection is what keeps the two
in step.

Errors should look like Azure's — an `x-ms-error-code` header and an XML
body:

```xml
<?xml version="1.0" encoding="utf-8"?>
<Error><Code>BlobAlreadyExists</Code><Message>…</Message></Error>
```

The SDK surfaces the code as `RestError.code`, so this is what makes the
client's error handling exercise its real branches.

## 4. CORS — and a discrepancy to escalate

The stand-in serves a different origin from the SPA, so it needs its own
CORS configuration mirroring the storage account rule in
[`../client-azure-upload.md`](../client-azure-upload.md) §8: allow the exact
dev origin (`http://localhost:5173`), never `*`; methods `PUT` and
`OPTIONS`; expose `etag` and `x-ms-request-id`.

While implementing this, note that the measured requests in §0 carry
**`x-ms-version` and `x-ms-client-request-id`** on every call, and §8's
allowed-headers list names neither. Both appear in the preflight's
`Access-Control-Request-Headers`, so the rule as currently specified looks
like it would fail preflight in production — surfacing exactly as the
"opaque network error with no useful detail" that §8 warns about.

**Flag this; do not quietly fix it.** The rule is an operator action item in
[`2_azure_resources.md`](completed/2_azure_resources.md) §4 and is not
applied yet. Write it up in the task's completion notes so the correction
lands in the operator's list, and configure the local stand-in to accept the
full set so the local flow works.

## 5. The client does not change

**No file under `client/src/` should need editing.** The client already
takes `upload_url` from the API and hands it to `BlockBlobClient`, which
happily talks to any host. If this task starts requiring client changes,
something has gone wrong in the backend — stop and reconsider rather than
adding a dev branch to client code.

It follows that `client/dist/` needs no rebuild and no commit, and that
`tests/auth.build.test.js` is unaffected.

The only client-side change is configuration: `.env.local` already carries
`VITE_DEV_TOKEN`, and nothing is added to it.

## 6. Tooling

Add to [`../../../.vscode/launch.json`](../../../.vscode/launch.json):

- **"Uploader dev blob store (local filesystem)"** — runs the §1 process on
  `127.0.0.1:8004`.
- **"Uploader API (local blob store + fake auth, offline)"** — the existing
  offline config with `UPLOADER_SAS_ISSUER=local`.
- **"Uploader: full stack (offline, real uploads)"** — a compound of those
  two plus the Chrome config. Make this the `order: 1` presentation entry;
  it becomes the day-to-day one.

Keep every existing configuration. The `fake` issuer stays useful for tests
and for starting the API alone.

## 7. Guardrails

Existing ones stay. `UPLOADER_AUTH_PROVIDER=fake` with
`UPLOADER_SAS_ISSUER=azure` is still refused at startup. Add:

- The `local` issuer refuses to start if its root cannot be created or
  written — a fail-fast at startup, per §4.8 of the brief, not an error at
  first upload.
- `uploader/api/**` never imports `uploader/devblob/**` (asserted by test).
- `UPLOADER_LOCAL_BLOB_ROOT` and `UPLOADER_LOCAL_BLOB_ENDPOINT` have no
  effect under the `azure` issuer.

## 8. Tests

`unittest`, not pytest. No test may touch the network or need credentials.

`LocalFileSasIssuer`:

- Round trip: commit a blob, read its properties back, list it.
- Listing returns only the caller's prefix, with the
  `bob@example.com` / `bob@example.com.au` case named explicitly — the same
  trailing-slash invariant as `GET /files`.
- Staged blocks never appear in a listing.
- Issued URLs point at the local endpoint, and `az_path` still points at the
  real configured container.
- A traversal attempt in the blob path is refused.

The store and server:

- Stage three blocks out of order, commit, and assert the bytes are exactly
  the original — ordered by the `BlockList`, not by arrival or filename.
- Blocks staged under one SAS commit successfully under a second, later SAS
  (the renewal path).
- A second commit to the same path is a `409` with `BlobAlreadyExists`.
- A request whose `se=` has passed is a `403`.
- An unrecognised `comp=` value is a `400`.
- A traversal path is a `400` and writes nothing outside the root.
- Preflight returns the headers the measured requests in §0 require.
- The content type from `x-ms-blob-content-type` reaches the sidecar and
  comes back through `get_blob_properties()`.

Then run the full backend suite, the frontend suite, and flake8
(`/home/cameron/.local/envs/claude/bin/flake8`).

## 9. Verification

The point of the task, so do it explicitly and report what was seen: launch
the new compound, upload a real multi-block file (>4 MiB, so block staging
and the concurrency pool are genuinely exercised), and confirm the progress
bar advances, the upload reaches `complete`, reconciliation passes against
the real bytes on disk, the file appears in the existing-files list with the
right size and content type, and the `az://` path renders.

Then confirm the failure paths render: upload the same file twice for the
create-only `409`.

## 10. Documentation

- [`../../README.md`](../../README.md) — replace "What can and cannot be
  exercised locally", which currently says the byte transfer does not work.
  It will. Keep the list of what is still only verifiable in production:
  real SAS signature validation, the real storage account's CORS rule, and
  Azure's seven-day garbage collection of uncommitted blocks.
- [`../../api/README.md`](../../api/README.md) — the two new variables and
  the third issuer value in the environment tables.
- [`../01-api.md`](../01-api.md) — the issuer list, if it enumerates them.

## 11. Out of scope

- **Azurite.** Exact protocol fidelity, but it needs Docker, real SAS
  signing with the well-known key, and relaxing the deliberately hardcoded
  blob endpoint. Not worth it for a two-request surface.
- Validating SAS signatures, permissions or resource scope. The stand-in
  honours expiry and nothing else.
- Any change to the authorisation rule, the SAS parameters, or the
  production configuration.
- Cross-session resume, still out of scope for the reason in
  [`4_list_and_renew_endpoints.md`](completed/4_list_and_renew_endpoints.md)
  §5: recovering staged block IDs needs `r` on the SAS, and create-only is
  worth more than the feature.
- Deleting abandoned blobs, local or remote.
- Serving blobs back out over `GET`. The client never reads them, and
  adding it would invite `sp=c` drifting toward `sp=rw`.
