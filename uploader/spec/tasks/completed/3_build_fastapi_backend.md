# Task 3 — Build the FastAPI backend

**Status:** ready to start. Partially unblocked — see §1.
**Owner:** agent (implementation), with the operator supplying Azure credentials
when [2_azure_resources.md](2_azure_resources.md) is actioned.
**Implements:** §6, §7, §10 and §11 of
[client-azure-upload.md](../client-azure-upload.md).
**Builds on:** [`uploader/api/cloudgene_auth.py`](../../api/cloudgene_auth.py),
which is already written, tested against real recorded responses, and is not to
be rewritten.

> ## ⛔ No agent shell access to production
>
> The same rule as [Task 1](1_investigate_user_auth.md) applies in full. **No
> `ssh cloudgene`**, no remote command execution on the production host, by any
> route. `cloudgene.qcif.edu.au` is a live service operated by people.
>
> `GET` requests to `https://cloudgene.qcif.edu.au` are permitted, as any client
> would make. Anything needing a shell gets written up as commands for the
> operator to run.
>
> **Do not deploy anything.** This task ends with code and tests in the repo.
> Installing the systemd unit, editing nginx config, and restarting services are
> operator actions — write them up, don't perform them.

## 1. What is and isn't blocked

The Azure resources do not exist yet: there is no container, no service
principal, and therefore no credential to sign a SAS with. **This does not block
the task**, provided the Azure dependency is isolated behind an interface from
the start (§4.3). Everything else — auth, request validation, blob naming,
persistence, the error taxonomy, rate limiting, the HTTP surface — is fully
buildable and fully testable today against a fake signer.

| Area | Status |
|---|---|
| Cloudgene auth | **Done** — `cloudgene_auth.py`, 37 tests passing |
| HTTP surface, validation, naming, persistence | **Unblocked** — build now |
| SAS signing against real Azure | **Blocked** on §3 of [2_azure_resources.md](2_azure_resources.md) — build behind an interface, test with a fake |
| Deployment (systemd, nginx) | **Out of scope** — write the files, don't install them |

Getting this ordering right is the point: if Azure SDK calls are scattered
through the request handlers, none of the logic can be tested until a client
secret exists. If they sit behind one small interface, the secret becomes a
late, low-risk substitution.

## 2. Decisions already settled

Do not relitigate these; they came out of Task 1 and the operator's direction.

- **Authorisation rule:** `loggedIn == true` and `apps` non-empty. Access to any
  workflow is the authorisation — untrusted users are granted no workflow at
  all. Do not pattern-match on app names.
- **`deprecatedApps` and `experimentalApps` do not count.** The response carries
  three app lists; only `apps` contributes. A user entitled solely to a
  deprecated or experimental app is denied, and that denial is intended. Do not
  "fix" this by summing the lists.
- **Identity is `user.mail`** from Cloudgene's response, lowercased. **Never**
  the JWT's `sub` (that is the username), and never anything from the request
  body.
- **The JWT is never parsed.** It is an opaque string to be forwarded.
- **Fail closed everywhere.** No code path returns a permissive default.
- **No caching of the identity.** The spec's §5.3 allows a ≤60s TTL, but
  loopback validation measured **6.6 ms** against 62 ms over public HTTPS. At
  that cost the cache buys nothing and delays revocation by up to a minute.
  Drop it, and update spec §5.3 to say so.
- **An account with no email is a `403`, not a `503`** — `NoUserEmailError`
  already models this. Cloudgene permits emailless accounts.

There are no open authorisation questions. If the implementation appears to need
one answered, that is a signal to stop and ask, not to decide.

## 3. Module layout

Everything under [`uploader/api/`](../../api/), flat — this is a small service
and a package hierarchy would be overhead. The venv is at `uploader/venv`
(Python 3.12).

```
uploader/api/
  app.py               FastAPI app, routes, exception handlers, startup
  cloudgene_auth.py    (exists — do not rewrite)
  azure_sas.py         SAS signing, behind a protocol (§4.3)
  storage.py           pending-upload records (§4.4)
  naming.py            blob path construction and validation (§4.2)
  config.py            env var loading, fail-fast on missing required values
  requirements.txt     add: fastapi, uvicorn[standard], azure-identity,
                       azure-storage-blob
  tests/
    test_cloudgene_auth.py   (exists)
    test_app.py              route-level, via TestClient
    test_naming.py
    test_storage.py
    test_azure_sas.py
    fixtures/                (exists — real recorded Cloudgene responses)
```

House style per `CLAUDE.md`: constants at the top below imports, no trailing
whitespace, the documented wrapping style. **Run flake8 before finishing** —
`/home/cameron/.local/envs/claude/bin/flake8` if the venv lacks it. Tests are
`unittest`, not pytest, and run from the `uploader/api` directory.

## 4. What to build

### 4.1 HTTP surface

FastAPI app with `root_path="/uploads/api"` (§6 of the spec explains why: nginx
strips the prefix via the trailing slash on `proxy_pass`).

| Route | Purpose |
|---|---|
| `POST /uploads` | Validate, reserve, issue a SAS. The endpoint from §7 of the spec. |
| `POST /uploads/{upload_id}/complete` | Client reports completion; triggers reconciliation (§4.5). |
| `GET /uploads/{upload_id}` | Status of one upload, owner-scoped. |
| `GET /healthz` | Liveness. Must **not** require auth and must **not** call Cloudgene — it is for the process, not the dependency chain. |

Every route except `/healthz` reads `X-Auth-Token` and resolves identity through
`validate_token()`. Make that a FastAPI dependency so it cannot be forgotten on
a new route.

`GET /uploads/{id}` must be scoped to the resolved email, not just to the upload
ID. An upload ID that leaks otherwise becomes an authorisation bypass.

### 4.2 Blob naming (`naming.py`)

The server owns the prefix; the client proposes the leaf. Per §7 of the spec the
path is `container/<user-email>/<client path>`.

This is the function most worth being paranoid in, because the prefix is the
only thing separating users in a shared container:

- Lowercase and strip the email. It comes from Cloudgene, but normalise anyway.
- Reject client paths containing `..`, a leading `/`, a backslash, a NUL, or any
  control character. Reject absolute-looking and URL-encoded traversal
  (`%2e%2e`, `..%2f`) — decode first, then check, or refuse encoded input
  outright.
- Normalise the result and **assert** it still starts with the expected prefix.
  A belt-and-braces check after normalisation catches what the blacklist missed.
- Enforce a maximum total path length (Azure's limit is 1024 characters) and a
  maximum segment count.

Test this directly with a traversal corpus, not only through the route.

### 4.3 Azure SAS (`azure_sas.py`) — behind an interface

Define a narrow protocol, something like:

```python
class SasIssuer(Protocol):
    def issue(self, blob_path: str, expiry: datetime) -> IssuedSas: ...
```

Two implementations: `AzureUserDelegationSasIssuer` (real) and
`FakeSasIssuer` (tests, and local development without credentials). Select by
config. The real one:

- Authenticates with `ClientSecretCredential` from `AZURE_TENANT_ID`,
  `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (§3 of
  [2_azure_resources.md](2_azure_resources.md)).
- Calls `get_user_delegation_key`, **caches it in process** with refresh well
  before its expiry (max lifetime 7 days), and on a stale-key failure
  re-requests and retries **once** — the spec calls for exactly one retry, not a
  loop.
- Signs a SAS with the §7 parameters: single blob (`b`), permission `c`
  (create) only, 24-hour expiry, start time 5 minutes in the past for clock
  skew, HTTPS only, no IP range.

Assert those parameters in tests. `c` versus `w` is the difference between "can
create this blob once" and "can overwrite anything it names"; a future edit that
widens it should break a test loudly.

Since there are no credentials yet, the real implementation cannot be executed.
That is acceptable — write it, keep it thin, and make clear in the report that
it is the one unexercised path.

### 4.4 Pending-upload records (`storage.py`)

§7 requires a record at issuance: upload ID, user email, blob path, declared
size and content type, issued-at, expiry, state.

**Use SQLite**, in a file under the service's working directory. `sqlite3` is
stdlib, so this adds no dependency and no daemon — operationally it is one file
on disk, the same weight as a directory of JSON.

The reason it wins over flat files is not durability (a JSON file per upload,
written with `os.replace`, is perfectly durable) but that two of the
requirements are **queries across records**, not lookups by ID:

- Rate limiting (§4.6) — count issuances for one user within a rolling window.
- The expiry sweep — find records where `state = 'pending' AND expiry < now`.

With flat files each of those is a directory scan, and every future question of
that shape adds another. Get/put by ID, which is everything else, would have
favoured flat files.

Two things this obliges, both easy to omit:

- **Set `journal_mode=WAL` and a `busy_timeout`** (a few seconds). Without them,
  concurrent writers under multiple uvicorn workers raise
  `sqlite3.OperationalError: database is locked` — intermittently, and usually
  first in production. This is the one real cost of the choice; do not skip it.
- **Keep the access layer narrow** — a handful of functions, no ORM, no SQL in
  the route handlers — so the store could be swapped without touching anything
  else.

Then:

- Upload IDs must be unguessable (`secrets.token_urlsafe`), not sequential.
- Every read is filtered by owner email.
- State transitions are explicit: `pending` → `completed` | `failed` |
  `expired`. Reconciliation must be **idempotent** — §12 of the spec requires a
  duplicate delivery to produce a single outcome. Do this with a conditional
  `UPDATE ... WHERE state = 'pending'` and check the affected row count, rather
  than read-then-write, which races.

### 4.5 Completion and reconciliation

Event Grid is deferred (§6 of [2_azure_resources.md](2_azure_resources.md)), so
completion is client-reported: the client calls
`POST /uploads/{id}/complete`, and the backend reads the blob's real properties
and reconciles against the record — size, content type, existence.

Treat the client's report purely as a **trigger**, never as evidence. Nothing in
the request body is trusted; the verdict comes from the blob properties read
server-side. A client that never calls back should leave a `pending` record that
an expiry sweep can later mark `expired`.

Structure this so an Event Grid webhook could call the same reconciliation
function later without rework.

### 4.6 Rate limiting

§7 and §10 require per-user limits on token issuance — a compromised account
otherwise mints unlimited write capabilities. Key on the resolved email, not on
IP. A simple counter in SQLite over a rolling window is sufficient; the point is
blast-radius control, not precision.

### 4.7 Error taxonomy

One exception handler mapping, so no route invents its own:

| Exception | Status | Body |
|---|---|---|
| `NotAuthenticatedError` | 401 | Generic; client redirects to Cloudgene login |
| `NoUserEmailError` | 403 | **Surface the message** — it tells the user what to fix |
| Not entitled (`is_authorised` false) | 403 | Explanatory, no SAS |
| `CloudgeneUnavailableError` | 503 | Generic |
| `CloudgeneContractError` | 503 | Generic, and log loudly — it means Cloudgene changed |
| Validation failure (size, type, path) | 400 | Which check failed |
| Rate limit exceeded | 429 | With `Retry-After` |

Never leak the token, the client secret, a SAS query string, or an internal
traceback into a response body. Log the auth decision and reason per §11 —
**never the token itself**.

### 4.8 Startup

Call `startup_self_test()` on startup (§5.3). Validate configuration eagerly and
**refuse to start** on a missing required variable, rather than failing on the
first user request. A service that starts misconfigured and 500s on demand is
worse than one that won't start.

## 5. Tests

`unittest`, run from `uploader/api`. Mirror the existing style in
[`tests/test_cloudgene_auth.py`](../../api/tests/test_cloudgene_auth.py) — real
recorded fixtures where they exist, no network in the test suite.

Cover at minimum:

- Each row of the §4.7 taxonomy, driven from the recorded fixtures
  (`anonymous.json`, `unentitled.json`, `authorised.json`).
- The traversal corpus from §4.2.
- SAS parameters: permission is exactly `c`, resource is `b`, expiry honoured.
- Reconciliation idempotency: calling complete twice yields one outcome, and
  the second call reports the same state rather than erroring.
- The SQLite store opens with WAL enabled and a non-zero busy timeout.
- Owner scoping: user A cannot read user B's upload by ID.
- A missing-config startup refuses to start.
- Rate limit trips and recovers.

No test may require Azure credentials or reach the network.

## 6. Deliverables

1. The modules in §3, passing flake8 and the full test suite.
2. `requirements.txt` updated, and the venv at `uploader/venv` synced.
3. A systemd unit `uploader/uploads.service`, modelled on
   [downloader/downloads.service](../../../downloader/downloads.service):
   `www-data`, port 8003, `Restart=on-failure`, the same hardening block, and
   **secrets via `EnvironmentFile` outside the repo** — never inline, since the
   unit file is world-readable.
4. The nginx blocks from §6 of the spec, as a snippet for the operator to apply
   — **not** applied.
5. A short `uploader/api/README.md`: env vars, how to run locally with the fake
   issuer, how to run the tests.
6. A report covering: anything in the spec contradicted by implementation, and
   an explicit list of what could not be exercised without Azure credentials.

## 7. Out of scope

The Vue frontend (§9 of the spec), Event Grid (§6 of
[2_azure_resources.md](2_azure_resources.md)), deployment, and any change to
Cloudgene itself.
