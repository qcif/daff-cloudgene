# Uploader API

FastAPI backend for direct browser-to-Azure Blob Storage uploads. It issues
short-lived, single-blob, create-only SAS tokens to users authenticated
against the existing Cloudgene session, and reconciles what actually landed
in the container against what was promised.

The file bytes never pass through this service. See
[`../spec/client-azure-upload.md`](../spec/client-azure-upload.md) for the
design and [`../spec/tasks/3_build_fastapi_backend.md`](../spec/tasks/3_build_fastapi_backend.md)
for the build brief.

## Modules

| File | Purpose |
|---|---|
| `app.py` | Routes, dependencies, the error taxonomy, startup |
| `cloudgene_auth.py` | Token validation against Cloudgene's `/api/v2/server` |
| `azure_sas.py` | SAS signing, behind a protocol; real and fake issuers |
| `storage.py` | Pending-upload records in SQLite (WAL + busy_timeout) |
| `naming.py` | Blob path construction and traversal defence |
| `config.py` | Environment loading; fails fast on anything missing |

## Routes

All paths below are relative to `https://cloudgene.qcif.edu.au/uploads/api`.

| Route | Auth | Purpose |
|---|---|---|
| `POST /uploads` | `X-Auth-Token` | Validate, reserve a blob path, return a SAS URL |
| `POST /uploads/{id}/complete` | `X-Auth-Token` | Trigger reconciliation against the real blob |
| `POST /uploads/{id}/renew` | `X-Auth-Token` | Re-sign the same blob path and extend `expires_at` |
| `GET /uploads/{id}` | `X-Auth-Token` | Status of one upload, owner-scoped |
| `GET /files` | `X-Auth-Token` | List the caller's own blobs, read from Azure and annotated with matching records |
| `DELETE /files` | `X-Auth-Token` | Delete one blob from the caller's own prefix |
| `GET /healthz` | none | Liveness of this process only |

`POST /uploads` takes `{"filename": ..., "size": ..., "content_type": ...}`.
Any other field in the body is ignored — identity comes from Cloudgene's
`user.mail`, never from the request.

`POST /uploads/{id}/renew` takes no body. It only succeeds while the record
is still `pending`, and is bounded by a per-record cap
(`UPLOADER_MAX_RENEWALS`); a terminal record is `409`, a spent cap is `429`.

`GET /files` takes no parameters — the prefix is always the caller's own
(`naming.prefix_for(email)`), never influenced by the request. The result
is capped at `UPLOADER_MAX_LIST_RESULTS` with a `truncated` flag rather than
paginated. Each entry carries `blob_path`, `client_path`, `az_path`, `size`,
`content_type` and `last_modified`, plus `upload_id` and `state` when a
record matches. `client_path` is the portion after the caller's prefix — the
value `DELETE /files` takes back, sent from here so the client never derives
it by slicing a blob path.

`DELETE /files` takes `{"client_path": "reads_R1.fastq.gz"}` — a path
*within* the caller's own prefix, never a blob path. The server rebuilds the
full path with `naming.build_blob_path(email, client_path)`, so naming
another user's blob is not rejected so much as unexpressible, and every
traversal defence in `naming.py` applies to deletion unchanged. The
deletion runs server-side under the service principal, because every SAS
this service issues is create-only (`sp=c`) and widening it to `d` would
mean a leaked SAS could destroy data.

It is idempotent: a blob that is already gone is `200` with
`{"deleted": false, ...}`, not `404`. It is refused with `409` while an
unexpired `pending` record exists for the path, since deleting does not
revoke the outstanding SAS and an in-flight upload would re-create the blob
afterwards. Records are never mutated by a delete — they are the issuance
history, while the container is the truth about what exists.

**Deletion is permanent.** Nothing in [`../azure.md`](../azure.md) enables
blob soft delete or versioning on the storage account, so assume there is no
undelete until an operator confirms otherwise:

```sh
az storage account blob-service-properties show \
  --account-name "$AZURE_STORAGE_ACCOUNT" \
  --query deleteRetentionPolicy
```

Errors: `401` not signed in, `403` no workflow access or no email on the
account, `400` a failed validation check, `404` no such upload (or not
yours), `409` a renewal on a terminal record or a delete racing a live
upload, `429` rate limited or a spent renewal cap (with `Retry-After` on the
former), `503` Cloudgene or Azure unavailable.

There is no rate limit on deletion: a delete costs one Azure call and can
only ever touch the caller's own prefix, whereas the issuance limiter exists
because a SAS is a capability handed out.

## Environment variables

Required — the service **refuses to start** without them:

| Variable | Notes |
|---|---|
| `AZURE_STORAGE_ACCOUNT` | e.g. `daffstandard` |
| `AZURE_STORAGE_CONTAINER` | e.g. `uploads` |
| `AZURE_TENANT_ID` | Required only when `UPLOADER_SAS_ISSUER=azure` |
| `AZURE_CLIENT_ID` | ditto |
| `AZURE_CLIENT_CERTIFICATE_PATH` | ditto. Path to the PEM holding the private key and certificate. **Never** in the repo or the unit file; must not be world-readable, or the service will refuse to start |

The service principal authenticates with a **certificate**, not a client
secret — see [../azure.md](../azure.md) for creation, rotation and
revocation. The certificate expires three years after creation.

Optional:

| Variable | Default | Notes |
|---|---|---|
| `UPLOADER_SAS_ISSUER` | `azure` | `fake` for local development with no real byte transfer; `local` for local development where uploads really land on disk — see [`../devblob/README.md`](../devblob/README.md) |
| `UPLOADER_AUTH_PROVIDER` | `cloudgene` | `fake` answers auth from `tests/fixtures/` instead of calling Cloudgene, and skips the startup contract check. Local development only; **refused at startup** alongside `UPLOADER_SAS_ISSUER=azure` |
| `CLOUDGENE_BASE_URL` | `https://cloudgene.qcif.edu.au` | Set to `http://127.0.0.1:8082` in production |
| `UPLOADER_DB_PATH` | `uploads.sqlite3` | Relative to the working directory |
| `UPLOADER_MAX_UPLOAD_BYTES` | 500 GiB | Advisory; a SAS cannot enforce size |
| `UPLOADER_SAS_TTL_SECONDS` | `86400` | 24 hours |
| `UPLOADER_RATE_LIMIT_MAX` | `200` | Token issuances per user per window |
| `UPLOADER_RATE_LIMIT_WINDOW_SECONDS` | `3600` | |
| `UPLOADER_ALLOWED_EXTENSIONS` | see `config.py` | Comma-separated |
| `UPLOADER_ALLOWED_CONTENT_TYPES` | see `config.py` | Comma-separated |
| `UPLOADER_MAX_LIST_RESULTS` | `1000` | Cap on `GET /files`; excess sets `truncated` rather than paginating |
| `UPLOADER_MAX_RENEWALS` | `10` | Per-record cap on `POST /uploads/{id}/renew` |
| `UPLOADER_LOCAL_BLOB_ROOT` | `/tmp/uploader-blobs` | Honoured only when `UPLOADER_SAS_ISSUER=local`. Where blobs land; proven writable at startup |
| `UPLOADER_LOCAL_BLOB_ENDPOINT` | `http://127.0.0.1:8004` | Honoured only when `UPLOADER_SAS_ISSUER=local`. Host embedded in issued SAS URLs — the `uploader/devblob` process |
| `UPLOADER_LOG_LEVEL` | `INFO` | |

The three Azure credentials are supplied by an `EnvironmentFile` outside the
repo — see [`../deploy/uploads.service`](../deploy/uploads.service). A systemd unit file is
world-readable, so a secret inlined there is a secret published.

In production every setting above always comes from the real process
environment. For local development, the same variables can instead be put
in `uploader/.env` (see `../.env.sample`) — gitignored, never committed,
the same file used for az-cli resource creation in `../azure.md` — and
`load_config()` picks it up automatically via `python-dotenv`, no sourcing
needed. A variable already set in the real environment is never overridden
by one from `.env`.

## Running locally

No Azure credentials needed. `UPLOADER_SAS_ISSUER=fake` substitutes an
issuer that emits a SAS with the same parameters as the real one
(`sp=c`, `sr=b`, `spr=https`) but a signature Azure will reject, and keeps
an in-memory table of "blobs" so the completion flow can be exercised.

Either export the variables directly, or write them once to
`uploader/.env`:

```sh
cat > uploader/.env <<'EOF'
UPLOADER_SAS_ISSUER=fake
AZURE_STORAGE_ACCOUNT=daffstandard
AZURE_STORAGE_CONTAINER=uploads
UPLOADER_DB_PATH=/tmp/uploads.sqlite3
EOF
```

```sh
cd uploader/api

UPLOADER_SAS_ISSUER=fake \
AZURE_STORAGE_ACCOUNT=daffstandard \
AZURE_STORAGE_CONTAINER=uploads \
UPLOADER_DB_PATH=/tmp/uploads.sqlite3 \
../venv/bin/uvicorn app:app --host 127.0.0.1 --port 8003 --reload
```

Startup still runs `startup_self_test()`, which makes one anonymous `GET`
to `CLOUDGENE_BASE_URL/api/v2/server` and refuses to start if the response
shape has changed. That request needs no credential, but it does need
network access to `cloudgene.qcif.edu.au` (or a reachable loopback
Cloudgene).

Interactive docs are at `http://127.0.0.1:8003/docs`. Note that `root_path`
is set to `/uploads/api` for the production reverse proxy, so the "try it"
URLs in the docs page will be wrong when running directly — call the paths
without the prefix.

## Tests

`unittest`, not pytest. Run from this directory:

```sh
cd uploader/api
../venv/bin/python -m unittest discover -s tests -t .
```

No test reaches the network and none requires Azure credentials. The
Cloudgene fixtures under `tests/fixtures/` are real response bodies
recorded from production on 2026-09-14; the auth tests run the real
decision logic over them rather than stubbing a verdict.

Lint:

```sh
/home/cameron/.local/envs/claude/bin/flake8 uploader/api
```

## Deployment

Not performed by this repo. [`../deploy/README.md`](../deploy/README.md) is
the operator runbook; nothing in [`../deploy/`](../deploy/) has been
applied.
