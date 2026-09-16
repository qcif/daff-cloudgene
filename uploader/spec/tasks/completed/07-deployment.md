# Task 7 — the deployment package

Turn the two loose config files into one reviewable package an operator can
install in a single pass, and close the gaps that only surface on a real
host. The uploader works end to end locally (task 6); nothing about it has
ever run on `cloudgene.qcif.edu.au`.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Everything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server. The deliverable here is a package and a runbook, not a deployment.

Read [`2_azure_resources.md`](completed/2_azure_resources.md) and
[`1_operator_runbook.md`](completed/1_operator_runbook.md) first. Their
settled decisions all still hold, and §6 below depends on several of them.

## 0. What already exists

Two files, written during earlier tasks, neither applied:

| File | Status |
|---|---|
| [`../../uploads.service`](../../uploads.service) | Complete, unapplied, at the top of `uploader/` |
| [`../../nginx-uploads.conf`](../../nginx-uploads.conf) | Complete, unapplied, same |

**This is not a rewrite.** Both are correct as far as they go, and every
decision baked into them was made for a reason that still applies:

- Cloudgene validated over loopback (`http://127.0.0.1:8082`), because the
  median was 6.6 ms and that is what let the identity cache be dropped.
- **No `client_max_body_size`** on `/uploads/api/`, unlike `/validation/api/`.
  That is the whole point of the design — file bytes never traverse nginx.
- Azure credentials in an `EnvironmentFile` outside the repo, because a unit
  file is world-readable.
- A certificate rather than a client secret.
- `client/dist/` committed, so deployment stays a `git pull` and the host
  needs no Node toolchain.

Preserve all of it. The work is consolidation plus the gaps below.

## 1. Shape: `uploader/deploy/`, with one runbook the operator reads

```
uploader/deploy/
    README.md            the runbook — prerequisites, install, verify, roll back
    uploads.service
    nginx-uploads.conf
    uploads.env.sample   every key, no values
    preflight.sh         checks the host is ready; changes nothing
    install.sh           optional, see below
```

`git mv` the two existing files in — do not copy them, and do not leave
stubs behind. Then fix every inbound link: `uploader/README.md`,
`uploader/api/README.md` (§Deployment, and the note near line 93),
`uploader/spec/01-api.md`, `uploader/spec/02-client.md` §10. A dead link in
an operator runbook is a deployment that stalls on a question the author
already answered.

**`README.md` is the deliverable the operator actually uses.** Everything
else in the directory is something it tells them to install. Write it so
that someone with root, this checkout, and no context can go from nothing to
a verified service without reading any other file in the repo — and without
guessing which of §6’s prerequisites are already done.

### On `install.sh`

Write it, with three constraints, or do not write it at all:

1. **Strictly idempotent, and never a silent overwrite.** If
   `/etc/uploads.env` already exists, it reports and leaves it alone. Same
   for the certificate and the unit.
2. **It refuses to run if `preflight.sh` fails.** A half-installed service is
   worse than an uninstalled one.
3. **Every step it takes is also written out longhand in `README.md`.** The
   script is a convenience, never the only path — an operator debugging a
   failed install must be able to read what it would have done.

Run `shellcheck` over both scripts. If a `python3` helper is easier than
shell for any step, use it and lint it with flake8 like everything else.

## 2. The checkout path appears in three files — settle it once

`nginx-uploads.conf` and `uploads.service` both hardcode
`/mnt/data/taxodactyl/cloudgene/`, while `config/cloudgene.service` uses
`/mnt/data/cloudgene`. These are different trees and the second is not this
repo, so the value is plausible but unconfirmed, and a wrong one fails three
different ways: nginx serves 404s for every asset, systemd fails with a
working-directory error, and the venv path in `ExecStart` fails with a
different error again.

Put it in exactly one place — a variable at the top of the runbook that the
operator sets once and which every subsequent command references — and have
`preflight.sh` assert the directory exists, is a checkout of this repo, and
contains `uploader/client/dist/index.html`. Confirming the real value is an
operator step; ask for it in the runbook rather than assuming.

## 3. The systemd unit

Keep: `User`/`Group` `www-data`, the loopback `CLOUDGENE_BASE_URL`, the
`EnvironmentFile`, `--workers=2`, and the whole hardening block.

Then work through the following. Each is a **decide and record** item, not
an instruction to change something — where you leave the current behaviour
alone, say in a comment why, so the next reader does not re-open it.

### 3.1 A misconfigured service must stop, not loop

The app is deliberately built to refuse to start when misconfigured — the
`lifespan` contract check, the fake-auth/azure-issuer interlock, the
certificate permission check. `Restart=on-failure` with no rate limit turns
every one of those into an infinite restart loop that buries the real error
under thousands of identical journal entries.

Add `StartLimitIntervalSec` and `StartLimitBurst` so it gives up and sits in
`failed` where `systemctl status` will show the reason.

### 3.2 Ordering against Cloudgene

`After=network-online.target` only. But `startup_self_test()` calls
`CLOUDGENE_BASE_URL/api/v2/server`, which in production is Cloudgene on
loopback — so at boot, `uploads.service` can easily come up before
`cloudgene.service` has bound port 8082 and fail its contract check for a
reason that has nothing to do with its own configuration.

Add the ordering dependency. Decide deliberately between `After=` alone
(ordering only) and `After=` + `Wants=` (also pulls it in), and pair it with
whatever §3.1 lands on so that a genuinely slow Cloudgene start is survived
by the retries rather than exhausting them.

### 3.3 `RestrictAddressFamilies` is missing `AF_UNIX`

The unit allows `AF_INET AF_INET6` only. Whether that is sufficient depends
on how the host resolves DNS: a glibc resolver talking to a nameserver over
UDP is fine, but NSS through `systemd-resolved` reaches it over a unix
socket, and the failure mode is that outbound HTTPS to
`login.microsoftonline.com` fails with a name-resolution error that looks
like a network problem rather than a sandbox problem.

`downloader/downloads.service` has the same line and evidently works, but it
never resolves an external hostname, so it is not evidence.

**Do not add `AF_UNIX` speculatively.** Determine it — the check is a
one-liner the operator can run before install, and belongs in
`preflight.sh`. Record the finding in the unit as a comment either way, so
nobody has to rediscover it.

### 3.4 Smaller additions

- `SyslogIdentifier=uploads`, so the journal files these logs under a name
  that means something rather than under `uvicorn`, and `journalctl -t`
  works. The runbook's verification commands should use it.
- `UMask=0077`. The SQLite database holds user email addresses and blob
  paths and is currently created with whatever the default umask gives.
- Run `systemd-analyze security uploads.service` on the host, record the
  score in the runbook, and take the suggestions that are cheap and safe
  (`ProtectKernelTunables`, `ProtectControlGroups`, `ProtectProc`,
  `PrivateDevices`, `SystemCallFilter=@system-service` are the usual ones).
  **Do not chase the score.** A sandbox directive that breaks the Azure SDK
  at runtime costs more than it buys; anything you cannot justify, skip and
  say so.
- Note explicitly that `EnvironmentFile=` is listed after the `Environment=`
  lines and therefore wins on conflict. Decide whether that override is
  wanted — it is a useful escape hatch for the operator, but it also means
  `/etc/uploads.env` can silently flip `UPLOADER_SAS_ISSUER` — and a unit
  shipping `local` or `fake` would start cleanly while writing every upload
  to a directory on the Nectar VM. Whichever way you decide, say so in a
  comment on the line, and make §9's journal check the thing that catches
  it: the startup log already prints the resolved issuer, and the `LOCAL`
  and `FAKE` warnings exist precisely so this is a glance, not an audit.

## 4. The nginx snippet

Keep both location blocks, the prefix-matching note, and the comment
explaining the deliberate absence of `client_max_body_size`.

### 4.1 Cache headers — the one that bites on the *second* deploy

Vite emits content-hashed assets (`dist/assets/index-DJPa0nXY.js`) alongside
an unhashed `index.html` that references them. With no cache headers, nginx
serves `index.html` with a validator that lets browsers and any intermediary
cache hold it — so after an upgrade, a returning user gets yesterday's
`index.html` pointing at an asset filename that no longer exists, and the
app is a blank page with a 404 in the console. The first deploy looks fine;
the second is where it appears.

Add:

- `/uploads/assets/` — immutable, long max-age. Safe precisely because the
  filenames are content-hashed.
- `/uploads/index.html` (and the `try_files` fallback that serves it) —
  `no-store`, or at minimum `no-cache` with revalidation.

### 4.2 `/docs` and `/openapi.json` are publicly reachable

`root_path="/uploads/api"` plus the proxy means FastAPI's interactive docs
and schema are served to the internet at
`https://cloudgene.qcif.edu.au/uploads/api/docs`. They expose no data and no
credentials, but they do advertise the full API surface to unauthenticated
callers, and nothing about the design needs them in production.

Decide, and recommend blocking them in nginx with a single `location` —
that needs no code change and is trivially reversible for debugging, where
disabling them in the app means a deploy to get them back.

### 4.3 `/healthz`

`GET /uploads/api/healthz` is unauthenticated by design (§4 of the API spec:
a health check that fails when a dependency restarts tells the supervisor to
kill a working process) and returns `{"status": "ok"}` and nothing else.
That is fine to expose — but **state it in the runbook explicitly** rather
than leaving it as something the operator discovers. It is also the one
post-install check an agent can run, per §9.

Confirm while you are there that nothing else routed through
`/uploads/api/` answers without authentication.

## 5. The environment file and the certificate

`uploads.env.sample`: every key the service reads in production, with no
values, each commented with where its value comes from —
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_CERTIFICATE_PATH` from
[`../../azure.md`](../../azure.md); the rest from
[`../../api/README.md`](../../api/README.md)'s table.

Check `git check-ignore -v uploader/deploy/uploads.env.sample` before
committing: `.gitignore` carries both `.env` and `.env.*`, and a sample file
that is silently ignored is a runbook step the operator cannot follow. The
name above is clear of both patterns; confirm rather than assume.

The install commands (already correct in the current unit's comments — lift
them into the runbook rather than rewriting them):

```sh
install -o root -g www-data -m 0640 /dev/null /etc/uploads.env
install -d -o root -g root -m 0755 /etc/cloudgene-uploader
install -o www-data -g www-data -m 0600 azure-cert.pem \
    /etc/cloudgene-uploader/azure-cert.pem
```

**Record the certificate's expiry as a literal date in the runbook**, and
name where the reminder lives. "Three years" in a comment is a date nobody
knows; an expired certificate takes uploads down with an error that does not
obviously say "expired".

## 6. Prerequisites that are not ours

These block deployment and none of them is code. List them at the top of the
runbook, with a checkbox each, because discovering a missing one halfway
through an install is how a maintenance window gets spent.

1. **The container exists.** [`2_azure_resources.md`](completed/2_azure_resources.md) §2.
2. **The service principal exists, with both role assignments** — `Storage
   Blob Delegator` at *account* scope and `Storage Blob Data Contributor` at
   *container* scope. Task 2 §3; a container-scoped Delegator fails at
   runtime with an authorisation error that looks nothing like a missing
   role.
3. **CORS on the storage account — done 2026-09-16.** The seven-header rule
   is applied; the four-header rule it replaced would have failed preflight
   on the first upload. Task 2 §4. This stays on the prerequisite list
   rather than being struck off it, because it is **the single most likely
   cause of a deployment that installs cleanly and does not work**, and
   because it is invisible from the server side — it surfaces only in the
   browser, as an opaque network error with no useful detail. The runbook
   should carry the credential-free `OPTIONS` probe from
   [`../../azure.md`](../../azure.md) §CORS designation as a pre-deploy
   check, so a rule that was cleared, edited or duplicated since is caught
   before anyone tries an upload.
4. **Egress from the Nectar VM** to `login.microsoftonline.com` and
   `daffstandard.blob.core.windows.net`. Task 2 §5.
5. **The venv is built on the host** — `python3 -m venv uploader/venv` and
   `pip install -r api/requirements.txt`. It is gitignored, so it is never
   part of a `git pull`; a fresh checkout has no venv and the unit's
   `ExecStart` fails with a path error. Pin the Python version the venv is
   built against in the runbook.
6. **Port 8003 is free.** Already confirmed (task 1 runbook, B8);
   `preflight.sh` should re-check rather than trust a months-old note.

## 7. What must not be deployed

State plainly in the runbook, because the answer to "should I copy this?" is
non-obvious for two of them:

- **`uploader/devblob/`** — it is present in the checkout and harmless there
  (nothing starts it, and `uploader/api/**` cannot import it, per the
  isolation test), but no unit, timer, or nginx location may reference it,
  and nothing should ever bind port 8004 on this host.
- **`venv/`** — built on the host, never copied from a workstation.
- **`*.sqlite3`** — a development database carries development records.
- **`.env`** — gitignored, and the production equivalent is
  `/etc/uploads.env`.

## 8. First deploy, upgrade, and rollback are three different procedures

Write all three. They share almost nothing, and conflating them is how an
upgrade ends up recreating `/etc/uploads.env`.

**First deploy** — install files, create the env file and certificate,
`daemon-reload`, `enable --now`, paste the nginx snippet, `nginx -t`,
`reload`, then §10.

**Upgrade** — `git pull`, `systemctl restart uploads`, and `nginx -t &&
systemctl reload nginx` *only* if the snippet changed. No Node step, because
`dist/` is committed — say so explicitly, since an operator who knows Vite
will look for one. Note the property that makes this cheap: **a restart does
not interrupt uploads in flight**, because the bytes are going from the
browser to Azure and not through this service. Only a request landing in the
restart window fails, and the client retries.

**Rollback** — `git checkout <previous tag>`, restart. Add the constraint
that makes this safe today and the condition under which it stops being
safe: the SQLite schema has no migrations, so an older binary reads a newer
database fine. If that ever changes, rollback needs a database step and this
paragraph is where it goes.

**The database** — say where it lives, that it is gitignored, and that
`git clean -xdf` in the checkout would delete real upload records along with
the venv. Give the backup one-liner (`sqlite3 … ".backup …"`, which is safe
against a running writer in WAL mode where a file copy is not) and tell the
operator to run it before an upgrade.

## 9. Verification

Split by who can do what. **The agent may only make read-only `GET`
requests to the public host.** Everything else is an operator step, written
as commands to run and expected output to compare against.

Operator, immediately after install:

- `systemctl status uploads` — active, and not in a restart loop.
- `journalctl -u uploads -n 50` — the startup line logs
  `auth_provider=cloudgene` and a redacted config showing `issuer: 'azure'`.
  **No `WARNING` mentioning FAKE or LOCAL.** Those warnings exist precisely
  so this check is a glance rather than an audit; quote the expected line in
  the runbook so a mismatch is obvious.
- `systemd-analyze security uploads.service` — record the score.

Agent or operator, over HTTPS:

- `GET /uploads/api/healthz` → `200`, `{"status": "ok"}`.
- `GET /uploads/` → the SPA; the hashed asset under `/uploads/assets/`
  returns `200` with a long `Cache-Control`, and `index.html` comes back
  uncached (§4.1).
- `GET /uploads/api/docs` → whatever §4.2 decided, and confirm it.

Operator, signed in to the real Cloudgene, with a real ~10 MiB file — the
same five checks as task 6 §9, which are now running against real auth and
real Azure for the first time:

1. The progress bar advances.
2. The upload reaches `complete`.
3. Reconciliation passes against the real blob.
4. The file appears in the existing-files list with the right size and
   content type.
5. The `az://` path renders.

Then upload the same file again and confirm the create-only `409` surfaces
as a clean failure, not a stall.

Finish the runbook with a short **"if it fails, look here first"** section:
the failed-preflight journal output, what a CORS rejection looks like from
the browser console (since it is the most likely failure and the least
self-describing), and what a missing or expired certificate logs.

## 10. Documentation to update

- `uploader/README.md` — the "Deployment — operator only" section becomes a
  pointer at `deploy/README.md` plus the table of what installs where.
- `uploader/api/README.md` §Deployment, and the `../uploads.service`
  reference near line 93.
- `uploader/spec/01-api.md` — the out-of-scope line naming both files.
- `uploader/spec/02-client.md` §10 — the `nginx-uploads.conf` path.
- Root `README.md` — the `./uploader/` section should mention the deploy
  package the way the `./config/` section describes its symlinks.

## 11. Out of scope

- **Ansible.** `sftp/` has a role; do not extend it to cover this. Two
  services deployed two ways is a smaller problem than a half-migrated
  Ansible tree.
- **Containers, CI/CD, deploy-on-push.** Deployment is `git pull` and a
  restart, by an operator, deliberately.
- **Blob cleanup for abandoned uploads** — task 2 §2.1. Real, separate, and
  a code change rather than a deployment one.
- **Event Grid `BlobCreated`** — task 2 §6, deferred.
- **Monitoring and alerting** beyond the journal. Note the one that carries
  real consequence and has no owner yet: **nothing alerts on the service
  principal's certificate approaching expiry.** Leave it out of this task,
  but name it in the runbook as the known gap, with the literal date from §5
  next to it.
