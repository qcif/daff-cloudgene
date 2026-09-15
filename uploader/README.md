# Azure Blob uploader

Lets authenticated Cloudgene users upload input data straight from the
browser to Azure Blob Storage. The file bytes never pass through this
server — the backend only authenticates the user, hands out a short-lived
create-only SAS for one blob, and afterwards reconciles what actually landed
against what was declared.

Served at `https://cloudgene.qcif.edu.au/uploads/`.

| Directory | What it is | Docs |
|---|---|---|
| [`api/`](api/) | FastAPI backend, listens on `127.0.0.1:8003` | [`api/README.md`](api/README.md) |
| [`client/`](client/) | Vue 3 + Vite SPA, built into `client/dist/` | this file, §Frontend |
| [`spec/`](spec/) | Design and build briefs | [`spec/01-api.md`](spec/01-api.md), [`spec/02-client.md`](spec/02-client.md) |

The two halves are deployed together but launched separately: the backend is
a long-running service, the frontend is static files built ahead of time and
served by nginx.

## Launching locally

You need two processes. Run them in separate terminals, backend first.

### 1. Backend

No Azure credentials needed — `UPLOADER_SAS_ISSUER=fake` substitutes an
issuer that emits structurally identical SAS URLs with a signature Azure
will reject, and keeps an in-memory blob table so the completion flow still
works:

```sh
cd uploader/api

UPLOADER_SAS_ISSUER=fake \
UPLOADER_AUTH_PROVIDER=fake \
AZURE_STORAGE_ACCOUNT=daffstandard \
AZURE_STORAGE_CONTAINER=uploads \
UPLOADER_DB_PATH=/tmp/uploads.sqlite3 \
../venv/bin/uvicorn app:app --host 127.0.0.1 --port 8003 --reload
```

The same variables can instead be written once to `uploader/.env`, which is
gitignored and picked up automatically — see
[`api/README.md`](api/README.md) for the full variable list and for the
`.env` form.

`UPLOADER_AUTH_PROVIDER=fake` answers auth from the response bodies recorded
against production in `api/tests/fixtures/`, instead of asking Cloudgene. It
substitutes for the HTTP call *only* — the verdict still comes from the real
`interpret_server_info()`, so the authorisation rule being exercised locally
is the one that runs in production. Four tokens are recognised:

| `VITE_DEV_TOKEN` | Cloudgene state it reproduces | Result |
|---|---|---|
| `dev-authorised` | Logged in, entitled, has an email | Everything works |
| `dev-unentitled` | Logged in, no workflow access | `403`, message shown verbatim |
| `dev-no-email` | Logged in, no email on the account | `403`, the one the user can fix |
| anything else | Absent, garbage or forged | `401` |

That last row matters: the real endpoint answers **HTTP 200 with
`loggedIn: false`** for absent, garbage and forged tokens alike, so the fake
does too. A fake that answered `401` there would hide client regressions
until production.

Omit `UPLOADER_AUTH_PROVIDER` and startup instead runs a self-test against
`CLOUDGENE_BASE_URL/api/v2/server`, **refusing to start** if that endpoint's
shape has changed. That needs no credential but does need network access to
`cloudgene.qcif.edu.au`; the fake provider skips it, which is what lets the
service start with no network at all.

> **`UPLOADER_AUTH_PROVIDER=fake` will not start with
> `UPLOADER_SAS_ISSUER=azure`.** Each is safe alone; together they would hand
> real Azure write capabilities to callers nobody authenticated, so the
> combination is refused at startup rather than trusted to a code review.

Interactive API docs: http://127.0.0.1:8003/docs. Note `root_path` is
`/uploads/api` for the production proxy, so the docs page's "try it" URLs
carry a prefix that is wrong when calling uvicorn directly — call the paths
without it.

### 2. Frontend

```sh
cd uploader/client

npm install
npm run dev
```

Opens on **http://localhost:5173/uploads/** — the `/uploads/` path matters,
because `base: '/uploads/'` in `vite.config.js` mirrors where nginx serves
the app in production. The dev server proxies `/uploads/api` to
`127.0.0.1:8003`, so request paths are identical in both environments.

### Getting a token into the dev server

The dev server runs on `localhost:5173`, a **different origin** from
Cloudgene, and `localStorage` is partitioned by origin — so the token the
app normally reads simply is not there.

Without one the page loads and says so, in a "Not signed in" banner naming
the variable below. It deliberately does **not** redirect to the login in
dev: there is no Cloudgene on this origin, so `/` is Vite's own SPA
fallback serving the same app straight back, and redirecting would bounce
the page against itself until the URL outgrew the request header limit.

The escape hatch is `VITE_DEV_TOKEN`, read only when `import.meta.env.DEV`
is set. With the fake auth provider running, it is not a credential at all —
just a string the backend maps to a recorded fixture:

```sh
cd uploader/client
echo 'VITE_DEV_TOKEN=dev-authorised' > .env.local
```

Swap in `dev-unentitled` or `dev-no-email` to see the two `403` states the
UI has to render, or any other string for the `401` path.

> Prefer this to a real token. You *can* paste a live JWT here instead —
> sign in to `cloudgene.qcif.edu.au` and copy the `token` field out of
> `localStorage['cloudgene']` — and you have to when running against the
> real Cloudgene. But it is then a live session credential that expires in 24
> hours and must stay out of commits, logs and fixtures. `.env.local` is
> gitignored (`.env.*`) either way.

The branch that reads `VITE_DEV_TOKEN` is compiled out of production builds
entirely; `tests/auth.build.test.js` runs a real build and greps the bundle
to prove it.

### What can and cannot be exercised locally

With both fakes running, the page comes fully to life as far as Azure: the
file picker and its accept list, client-side validation and its rejection
messages, the existing-files list, `POST /uploads`, and every auth state the
UI has to render.

What still does **not** work is the byte transfer. `FakeSasIssuer` builds
its URLs from the real blob endpoint, so the browser stages blocks against
`daffstandard.blob.core.windows.net` with an unsigned signature and gets a
`403`. Progress, reconciliation and the `az://` results list are therefore
unreachable locally — that is the one part still needing a manual check
against real credentials once the container is reachable.

Closing that gap would mean running Azurite and a third issuer that signs
with its well-known key, plus relaxing the deliberately hardcoded blob
endpoint in `config.py`. Deliberately not done.

## Tests

Backend — `unittest`, not pytest:

```sh
cd uploader/api
../venv/bin/python -m unittest discover -s tests -t .
```

Frontend — Vitest. No test touches the network or Azure:

```sh
cd uploader/client
npm run test
```

Lint the backend:

```sh
/home/cameron/.local/envs/claude/bin/flake8 uploader/api
```

## Building for deployment

`client/dist/` is **committed to the repo**, deliberately: nginx aliases
`/uploads/` straight at it in the deployed checkout, so deployment stays a
`git pull` and the production host needs no Node toolchain. The cost is
build output in version control and noisy diffs, and that is the accepted
trade (see [`spec/02-client.md`](spec/02-client.md) §10).

So after any change under `client/src/`:

```sh
cd uploader/client
npm run build
```

and commit the resulting `dist/` alongside the source change. A source
change committed without its rebuilt `dist/` deploys nothing.

## Deployment — operator only

**Not yet applied.** Both files are written for an operator to install; no
agent deploys, restarts or edits anything on `cloudgene.qcif.edu.au`, and
verification against the live host is read-only `GET` requests or manual
operator steps, never a shell.

| File | Installs as |
|---|---|
| [`uploads.service`](uploads.service) | `/etc/systemd/system/uploads.service` |
| [`nginx-uploads.conf`](nginx-uploads.conf) | pasted into the vhost, above the `location / ` Cloudgene proxy |

Three things must be true before the service will start, and each has been
made a hard failure rather than a silent degradation:

1. **The Azure credentials exist.** `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and
   `AZURE_CLIENT_CERTIFICATE_PATH` come from an `EnvironmentFile` at
   `/etc/uploads.env`, outside the repo — a systemd unit file is
   world-readable, so a secret inlined there is a secret published.
2. **The certificate is readable and not world-readable.** The service
   principal authenticates with a certificate, not a client secret; the PEM
   holds its private key. Provisioning and rotation are in
   [`azure.md`](azure.md). It expires three years after creation.
3. **Cloudgene answers on the configured base URL.** In production that is
   `http://127.0.0.1:8082` over loopback.

Separately, and easy to miss because it fails as an opaque browser network
error with no useful detail: **CORS is configured on the storage account**,
not in FastAPI. FastAPI's CORS middleware has no bearing on requests the
browser makes to `*.blob.core.windows.net`. The required rule — exact SPA
origins, never `*` — is in
[`spec/client-azure-upload.md`](spec/client-azure-upload.md) §8. Verify it
in isolation before debugging upload logic.
