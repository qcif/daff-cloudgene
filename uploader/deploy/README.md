# Deploying the Azure uploader

Operator runbook for `cloudgene.qcif.edu.au`. Requires root on that host,
this checkout, and no other file in the repo. Nothing here has been applied
yet — the uploader has never run on this host.

**No SSH to `cloudgene.qcif.edu.au` by an agent, ever.** Everything in this
file is written as commands for a human operator with a real shell. An
agent may only make read-only `GET` requests to the public host (§6).

## §0 — Before you start: three things to settle once

### The checkout path

`nginx-uploads.conf` and `uploads.service` both reference the checkout
directory as `@REPO_ROOT@` — a placeholder, not a real path. **Confirmed:**
this repo's checkout on `cloudgene.qcif.edu.au` is at
`/mnt/data/daff-cloudgene` — neither of the two other candidates considered
(`/mnt/data/taxodactyl/cloudgene`, an earlier guess, and `/mnt/data/cloudgene`,
which is `config/cloudgene.service`'s tree and not this repo at all). Set it
once, here, and use this variable in every command below:

```sh
REPO_ROOT=/mnt/data/daff-cloudgene
```

`preflight.sh` (§ below) still checks that `$REPO_ROOT` exists, is a
checkout of *this* repo, and contains `uploader/client/dist/index.html` —
run it before installing, even though the value above is now confirmed
rather than assumed.

### The prerequisites checklist

None of these is code, and none can be fixed mid-install. Confirm every box
before starting — discovering a missing one halfway through is how a
maintenance window gets spent.

- [ ] **The Azure Blob container exists.** [`2_azure_resources.md`](../spec/tasks/completed/2_azure_resources.md) §2.
- [ ] **The service principal exists, with both role assignments**:
      `Storage Blob Delegator` at *account* scope, `Storage Blob Data
      Contributor` at *container* scope. A container-scoped Delegator fails
      at runtime with an authorisation error that looks nothing like a
      missing role. Task 2 §3.
- [ ] **CORS on the storage account — applied 2026-09-16.** The
      seven-header rule (not the four-header rule it replaced) must be
      live. This is invisible from the server side — it surfaces only in
      the browser as an opaque network error — and is **the single most
      likely cause of a deployment that installs cleanly and does not
      work**. `preflight.sh` re-checks it with the credential-free
      `OPTIONS` probe from [`../azure.md`](../azure.md) §CORS designation,
      because a rule cleared, edited or duplicated since 2026-09-16 would
      not otherwise be caught before a real upload fails. Task 2 §4.
- [ ] **Egress from this VM** to `login.microsoftonline.com` and
      `daffstandard.blob.core.windows.net`. Confirmed once already (Task 1
      runbook, B7); `preflight.sh` re-checks it. Task 2 §5.
- [ ] **The venv is built on this host** — never copied from a
      workstation, and gitignored so `git pull` never brings one:

  ```sh
  python3.12 -m venv "$REPO_ROOT/uploader/venv"
  "$REPO_ROOT/uploader/venv/bin/pip" install -r \
      "$REPO_ROOT/uploader/api/requirements.txt"
  ```

  Pinned to **Python 3.12** (matches the development venv this was built
  and tested against).
- [ ] **Port 8003 is free.** Confirmed once already (Task 1 runbook, B8);
      `preflight.sh` re-checks rather than trusting a months-old note.
- [ ] **RestrictAddressFamilies / AF_UNIX determined.** `preflight.sh`
      reports which NSS module this host resolves hostnames through. If it
      reports the `resolve` module (systemd-resolved), add `AF_UNIX` to
      `uploads.service`'s `RestrictAddressFamilies` line **before** the
      first `daemon-reload` — see the comment on that line in the unit.
      Otherwise `AF_INET AF_INET6` is sufficient and no edit is needed.

Run the automated half of this checklist:

```sh
"$REPO_ROOT/uploader/deploy/preflight.sh" "$REPO_ROOT"
```

It checks the checkout, the venv, port 8003, the DNS resolver question,
Azure egress and the live CORS rule. It changes nothing. `install.sh`
refuses to run unless this passes.

### What installs where

| File | Installs as |
|---|---|
| [`uploads.service`](uploads.service) | `/etc/systemd/system/uploads.service` (with `@REPO_ROOT@` substituted) |
| [`nginx-uploads.conf`](nginx-uploads.conf) | pasted into the vhost, above the Cloudgene `location /` block (with `@REPO_ROOT@` substituted) |
| [`uploads.env.sample`](uploads.env.sample) | filled in and copied to `/etc/uploads.env` |
| the certificate you were given out of band | `/etc/cloudgene-uploader/azure-cert.pem` |

## §1 — First deploy

Either run [`install.sh`](install.sh) (below) or do it by hand — every step
it takes is written out here too.

```sh
sudo "$REPO_ROOT/uploader/deploy/install.sh" "$REPO_ROOT" /path/to/azure-cert.pem
```

`install.sh` is **strictly idempotent and never a silent overwrite**: if
`/etc/uploads.env`, the certificate, or the unit already exist, it reports
and leaves them alone. It refuses to run at all if `preflight.sh` fails —
a half-installed service is worse than an uninstalled one.

By hand, the same steps:

1. **Run preflight** (§0) and fix anything it flags.
2. **Create the environment file** (never overwrite an existing one):

   ```sh
   install -o root -g www-data -m 0640 /dev/null /etc/uploads.env
   ```

   Fill it in from [`uploads.env.sample`](uploads.env.sample):
   `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and
   `AZURE_CLIENT_CERTIFICATE_PATH` — from
   [`../azure.md`](../azure.md); everything else optional, described in
   [`../api/README.md`](../api/README.md)'s environment table.

3. **Install the certificate**, delivered out of band, never committed:

   ```sh
   install -d -o root -g root -m 0755 /etc/cloudgene-uploader
   install -o www-data -g www-data -m 0600 azure-cert.pem \
       /etc/cloudgene-uploader/azure-cert.pem
   ```

   **Record the certificate's expiry as a literal date here once it is
   created:**

   > Certificate expiry: **not yet created** — the service principal is
   > still an open prerequisite (§0). When
   > `az ad sp create-for-rbac --create-cert --years 3` is run (see
   > [`../azure.md`](../azure.md)), replace this line with the literal
   > date (creation date + 3 years) and set a calendar reminder against
   > it. Nothing currently alerts on this — see §8 "Known gaps".

4. **Install and start the unit**, with `@REPO_ROOT@` resolved:

   ```sh
   sed "s|@REPO_ROOT@|$REPO_ROOT|g" \
       "$REPO_ROOT/uploader/deploy/uploads.service" \
       | sudo tee /etc/systemd/system/uploads.service >/dev/null
   sudo systemctl daemon-reload
   sudo systemctl enable --now uploads
   ```

5. **Paste the nginx snippet**, with `@REPO_ROOT@` resolved, into
   `config/nginx-vhost.conf` and the live
   `/etc/nginx/sites-available/cloudgene.qcif.edu.au.conf` it mirrors,
   above the `location / { ... }` Cloudgene block:

   ```sh
   sed "s|@REPO_ROOT@|$REPO_ROOT|g" \
       "$REPO_ROOT/uploader/deploy/nginx-uploads.conf"
   # paste the output into the vhost by hand
   sudo nginx -t && sudo systemctl reload nginx
   ```

6. **Run §5 verification** below.

## §2 — Upgrade

An upgrade is **not** the first-deploy procedure — it must not recreate
`/etc/uploads.env` or touch the certificate.

```sh
cd "$REPO_ROOT" && git pull
sudo systemctl restart uploads
```

Reload nginx **only if `nginx-uploads.conf`'s content changed** in this
pull:

```sh
sudo nginx -t && sudo systemctl reload nginx
```

**No Node step.** `client/dist/` is committed to the repo, deliberately, so
`git pull` already brings the rebuilt frontend — an operator who knows Vite
will look for a build step here; there isn't one.

**A restart does not interrupt uploads in flight.** File bytes go from the
browser straight to Azure, never through this service, so only a request
that lands in the brief restart window fails — and the client retries it.

Back up the database first (§4).

## §3 — Rollback

```sh
cd "$REPO_ROOT" && git checkout <previous-tag>
sudo systemctl restart uploads
```

Safe **today** because the SQLite schema has no migrations — an older
binary reads a newer database without issue. **If a migration is ever
added, this stops being true**, and rollback needs an explicit database
step; this paragraph is where it goes when that happens.

## §4 — The database

`$REPO_ROOT/uploader/api/uploads.sqlite3` (per `UPLOADER_DB_PATH` in the
unit). Gitignored — it is never part of `git pull`, and **`git clean -xdf`
in this checkout would delete real upload records along with the venv**.
Do not run it here without checking what it would remove.

Back it up before every upgrade, safe against a running writer because the
database is in WAL mode (a plain file copy is not):

```sh
sqlite3 "$REPO_ROOT/uploader/api/uploads.sqlite3" \
    ".backup /var/backups/uploads-$(date +%F).sqlite3"
```

## §5 — Verification

Split by who can do what. **An agent may only make read-only `GET`
requests to the public host** — everything else here is an operator step.

### Operator, immediately after install or restart

```sh
systemctl status uploads          # active, not in a restart loop
journalctl -t uploads -n 50       # SyslogIdentifier=uploads
```

The startup line must read:

```
Starting uploader with auth_provider=cloudgene config {'issuer': 'azure', ...}
```

**No `WARNING` mentioning FAKE or LOCAL.** Both exist specifically so this
check is a glance, not an audit — `UPLOADER_AUTH_PROVIDER=fake` and
`UPLOADER_SAS_ISSUER=fake`/`local` each log one on startup if in effect.
Their presence means `/etc/uploads.env` (which overrides the unit, see the
comment on `EnvironmentFile=`) has flipped something it should not have.

```sh
systemd-analyze security uploads.service
```

Record the score here once run:

> `systemd-analyze security` score: **not yet run** — record here after
> the first install.

### Agent or operator, over HTTPS

```sh
curl -sS https://cloudgene.qcif.edu.au/uploads/api/healthz
# expect: 200, {"status": "ok"}
```

`GET /healthz` is unauthenticated by design (§4 of the API spec — a health
check that fails when a dependency merely restarts would tell the
supervisor to kill a working process), returns nothing but that literal
body, and is the one endpoint under `/uploads/api/` an agent can check
directly. Everything else under `/uploads/api/` requires `X-Auth-Token`.

```sh
curl -sS -o /dev/null -D - https://cloudgene.qcif.edu.au/uploads/
# expect: 200, the SPA's index.html

curl -sS -o /dev/null -D - \
    https://cloudgene.qcif.edu.au/uploads/assets/<some-hashed-file>.js
# expect: 200, Cache-Control: public, max-age=31536000, immutable

curl -sS -o /dev/null -D - https://cloudgene.qcif.edu.au/uploads/index.html
# expect: 200, Cache-Control: no-store

curl -sS -o /dev/null -w '%{http_code}\n' \
    https://cloudgene.qcif.edu.au/uploads/api/docs
# expect: 404 — interactive docs are blocked in production (§ nginx snippet)
```

### Operator, signed in to the real Cloudgene, with a real ~10 MiB file

The same five checks as task 6 §9, now running against real auth and real
Azure for the first time:

1. The progress bar advances.
2. The upload reaches `complete`.
3. Reconciliation passes against the real blob.
4. The file appears in the existing-files list with the right size and
   content type.
5. The `az://` path renders.

Then upload the same file again and confirm the create-only conflict
surfaces as a clean `409`, not a stall.

## §6 — What must not be deployed

- **`uploader/devblob/`** — present in the checkout and harmless there
  (nothing starts it, and `uploader/api/**` cannot import it, per the
  isolation test), but **no unit, timer, or nginx location may reference
  it**, and nothing should ever bind port 8004 on this host.
- **`venv/`** — built on the host (§0), never copied from a workstation.
- **`*.sqlite3`** — a development database carries development records.
- **`.env`** — gitignored; the production equivalent is `/etc/uploads.env`.

## §7 — If it fails, look here first

**Failed preflight.** `install.sh` refuses to run and prints which check
failed and why — read `preflight.sh`'s output rather than the systemd
journal.

**A CORS rejection**, the most likely failure and the least
self-describing: the browser console shows a generic network error on the
`PUT` to `*.blob.core.windows.net` with no status code and no response
body visible to JavaScript — nothing that says "CORS" explicitly. If an
upload never leaves the `uploading` state and the Network tab shows a
failed request with no response headers, suspect CORS first and re-run the
`preflight.sh` CORS probe (or [`../azure.md`](../azure.md) §CORS
designation's `curl` directly) before debugging anything in the app.

**A missing or expired certificate.** Startup logs (via
`journalctl -t uploads`) a `ConfigError` naming the certificate path
directly — "is not a file", "cannot be read", or "is world-readable" — for
a missing/unreadable/over-permissioned PEM. An *expired* certificate does
not fail at startup (the service only checks the file is readable, not the
certificate's validity) — it fails later, on the first SAS issuance after
expiry, as an Entra authentication error. This is why the expiry date in
§1 must be tracked outside the logs.

**A restart loop.** `systemctl status uploads` shows `failed` rather than
`activating` once `StartLimitBurst` is exhausted (§ unit comment) — read
the last real error in the journal before it, not the repeated ones.

## §8 — Known gaps

**Nothing alerts on the service principal's certificate approaching
expiry.** The date is tracked only in this file (§1) until a real
reminder exists — a calendar entry against the literal date once the
certificate is created is the minimum; monitoring it properly is out of
scope for this task. See §1 for the literal date once filled in.

**Blob cleanup for abandoned uploads**, **Event Grid `BlobCreated`**, and
**monitoring/alerting beyond the journal** are all deliberately out of
scope here — see [`../spec/tasks/completed/07-deployment.md`](../spec/tasks/completed/07-deployment.md)
§11.
