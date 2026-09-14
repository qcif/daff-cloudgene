# Task 1 — operator runbook

Everything here needs either a **shell on `cloudgene`** or a **browser / second
account**, so the agent cannot run any of it. Paste the blocks below, then fill
in the results tables at the bottom and hand them back.

Nothing in this runbook writes anything: no config changes, no restarts, no
package installs, no job submissions. All commands are `GET`s, reads and
listings. If a command appears to want to change something, do not run it —
raise it instead.

**Before you start:** capture a fresh `X-Auth-Token`. Log into
`https://cloudgene.qcif.edu.au`, open devtools → Network, right-click any
`/api/v2/...` XHR → *Copy as cURL*, and take the value of the `X-Auth-Token`
header. Tokens last 24 hours, so capture it immediately before running this.

**Do not paste the token value into this file or anywhere else in the repo.**
It is a live credential for your account until it expires.

```sh
TOKEN='<paste the X-Auth-Token value>'
```

---

## Part A — public API checks the agent could not run (§7.3–§7.7)

These go to the public HTTPS host and could be run from anywhere, including
your laptop. They are here only because they need a live token. If you would
rather hand the agent a fresh token and have it run these itself, that also
works — they are read-only `GET`s.

```sh
CG=https://cloudgene.qcif.edu.au/api/v2/server

# A1 (§7.3) — authorised body. Expect loggedIn true, your email, non-empty ids.
curl -sS "$CG" -H "X-Auth-Token: $TOKEN" \
  | jq '{loggedIn, mail: .user.mail, username: .user.username,
         admin: .user.admin, apps: [.apps[].id]}'

# A2 (§7.5) — corrupted signature. Expect loggedIn false.
#             This is what proves the signature is actually verified, rather
#             than the token merely being decoded.
curl -sS "$CG" -H "X-Auth-Token: ${TOKEN}x" | jq '{loggedIn, apps}'

# A3 (§7.5b) — payload tampered, signature left alone. Same expectation.
#              Flips one character in the middle segment of the JWT.
TAMPERED=$(python3 - "$TOKEN" <<'PY'
import sys
h, p, s = sys.argv[1].split('.')
mid = len(p) // 2
flip = 'B' if p[mid] == 'A' else 'A'
print(f"{h}.{p[:mid]}{flip}{p[mid+1:]}.{s}")
PY
)
curl -sS "$CG" -H "X-Auth-Token: $TAMPERED" | jq '{loggedIn, apps}'

# A4 (§7.6/§7.7) — minimum headers. Only X-Auth-Token is sent; no Referer,
#                  no X-Requested-With, no cookie. Expect the same result as
#                  A1. If it does NOT match A1, add headers back one at a time
#                  and record which one mattered.
curl -sS "$CG" -H "X-Auth-Token: $TOKEN" \
  | jq '{loggedIn, mail: .user.mail, apps: [.apps[].id]}'
```

### A5 (§7.8) — logout behaviour. **The single most useful answer here.**

1. With `$TOKEN` still set in the shell, go to the browser and **log out** of
   Cloudgene properly (the UI logout, not just closing the tab).
2. Back in the shell, re-run A1 with the same, now-logged-out token.

```sh
curl -sS "$CG" -H "X-Auth-Token: $TOKEN" | jq '{loggedIn, apps: [.apps[].id]}'
```

If this still returns `loggedIn: true`, logout is client-side only: the token
stays valid server-side until `exp`, revocation is not meaningful, and the
uploader's validation cache TTL is the only thing bounding how long a
logged-out token keeps working. That is an inherited Cloudgene property, not
something the uploader introduces — but it needs to be recorded.

You will need a fresh token for anything after this point.

### A6 (§7.9) — token renewal

Leave a logged-in Cloudgene tab open. In the devtools console, note the value
and check again later in the same session:

```js
JSON.parse(localStorage.getItem('cloudgene')).token.slice(-12)
```

Record whether the last 12 characters change before the 24 h expiry. Just the
tail — never the whole token.

### A7 (§7.10) — an unentitled account, only if one already exists

Run A1 with a token from an account that has no workflow access. Expect
`loggedIn: true` with `apps: []`. **Do not create an account on production for
this** — skip the check if no such account exists and say so. This is
corroboration of behaviour you have already confirmed, not a discovery.

### A8 (§7.11) — email semantics

From the Cloudgene UI, as a normal (non-admin) user: is there a field that lets
a user change their own email address? Yes or no, plus where it lives.

If yes, a user's blob prefix moves when they change it, and their earlier
uploads are stranded under the old prefix. We may accept that, but not
unknowingly.

### A9 — what Cloudgene passes to workflows as `CLOUDGENE_USER_EMAIL`

This is §9 of the task doc and is answerable from the repo or the running
config rather than from a shell, but it is cheap to settle while you are here:
does `CLOUDGENE_USER_EMAIL` receive the user's **email** or their **username**?
The uploader's blob prefix should agree with whatever job outputs use.

---

## Part B — shell on `cloudgene` (§8) (COMPLETED)

```sh
TOKEN='<paste a fresh token>'

# B1 (§8.1) Does Cloudgene answer on loopback, bypassing nginx?
#           Expect HTTP 200 and a JSON body.
curl -sS -i -m 5 http://127.0.0.1:8082/api/v2/server \
  -H 'Host: cloudgene.qcif.edu.au' \
  -H "X-Auth-Token: $TOKEN" | head -20

# B2 (§8.2) Same, without the Host header — does Cloudgene care?
curl -sS -o /dev/null -w '%{http_code}\n' -m 5 \
  http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"

# B3 (§8.2b) Anonymous on loopback — no token at all. Expect 200 with
#            loggedIn false. This is the request the uploader's startup
#            self-test makes, so it must work without a credential.
curl -sS -o /dev/null -w '%{http_code}\n' -m 5 \
  http://127.0.0.1:8082/api/v2/server

# B4 (§8.3) Latency over 20 sequential loopback calls; median is what matters.
#           For comparison, the agent measured a median of 0.062 s over the
#           public HTTPS path from an external network.
for i in $(seq 20); do
  curl -sS -o /dev/null -w '%{time_total}\n' \
    http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"
done | sort -n | awk '{a[NR]=$1} END {print "median", a[int(NR/2)+1]}'
```

Output from above:

```
#           Expect HTTP 200 and a JSON body.
curl -sS -i -m 5 http://127.0.0.1:8082/api/v2/server \
  -H 'Host: cloudgene.qcif.edu.au' \
  -H "X-Auth-Token: $TOKEN" | head -20

# B2 (§8.2) Same, without the Host header — does Cloudgene care?
curl -sS -o /dev/null -w '%{http_code}\n' -m 5 \
  http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"

# B3 (§8.2b) Anonymous on loopback — no token at all. Expect 200 with
#            loggedIn false. This is the request the uploader's startup
#            self-test makes, so it must work without a credential.
curl -sS -o /dev/null -w '%{http_code}\n' -m 5 \
  http://127.0.0.1:8082/api/v2/server

# B4 (§8.3) Latency over 20 sequential loopback calls; median is what matters.
#           For comparison, the agent measured a median of 0.062 s over the
#           public HTTPS path from an external network.
for i in $(seq 20); do
  curl -sS -o /dev/null -w '%{time_total}\n' \
    http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"
done | sort -n | awk '{a[NR]=$1} END {print "median", a[int(NR/2)+1]}'
HTTP/1.1 200 OK
date: Mon, 14 Sep 2026 04:46:38 GMT
Content-Type: application/json
content-length: 2038

{"name":"DAFF Biosecurity workflows","background":"#343a40","foreground":"navbar-dark","footer":"<p>\n  powered by <a href=\"http://cloudgene.uibk.ac.at\">Cloudgene</a>\n</p>\n\n<div class=\"d-flex flex-column align-items-center\" style=\"margin-top: -30px\">\n\n  <div>\n    Developed by\n  </div>\n\n  <div class=\"mb-3\">\n    <a href=\"https://qcif.edu.au\" target=\"_blank\">\n      <img\n        src=\"https://github.com/qcif/taxodactyl/raw/refs/heads/main/docs/images/qcif.svg\"\n        style=\"margin: auto; width: 250px\"\n      />\n    </a>\n  </div>\n\n  <div>\n    Funded by\n  </div>\n\n  <div class=\"row justify-content-center\">\n    <div class=\"col-auto mx-2\">\n      <a href=\"https://www.agriculture.gov.au/biosecurity-trade/policy\" target=\"_blank\">\n        <img\n          src=\"https://github.com/qcif/taxodactyl/raw/refs/heads/main/docs/images/daff-logo.svg\"\n          style=\"margin: auto; width: 200px\"\n        />\n      </a>\n    </div>\n    <div class=\"col-auto mx-2\">\n      <a href=\"https://biocommons.org.au\" target=\"_blank\">\n        <img\n          src=\"https://github.com/qcif/taxodactyl/raw/refs/heads/main/docs/images/Australian-Biocommons-Logo-Horizontal-RGB.png?raw=true\"\n          style=\"margin: auto; width: 200px\"\n        />\n      </a>\n    </div>\n  </div>\n</div>\n","emailRequired":true,"userEmailDescription":"Receive email notifications when jobs are completed.","userWithoutEmailDescription":"You can enter your email address at any time to upgrade your account.","oauth":[],"user":{"username":"cameron","mail":"chyde@neoformit.com","admin":false,"name":"Cameron Hyde"},"apps":[],"deprecatedApps":[],"experimentalApps":[],"loggedIn":true,"navigation":[{"id":"docs","name":"Docs","link":null,"items":[{"id":"taxodactyl","name":"Taxodactyl","link":"#!pages/taxodactyl","items":null}]},{"id":"input_validation","name":"Tools","link":null,"items":[{"id":"taxodactyl_validation","name":"Taxodactyl input validation","link":"/validation/","items":null}]}],"maintenace":false}200
200
median 0.006607
```

Ports:

```sh
# B8 (§8.6) Is port 8003 free for the uploader? Expect no output.
ss -lntp 2>/dev/null | grep ':8003' || echo 'port 8003 free'
# >>> port 8003 free

# B9 Confirm the ports already in use, so 8003 really is the right choice.
ss -lntp 2>/dev/null | grep -E ':(8000|8002|8003|8082)' || echo 'none listening'
# >>> LISTEN 0      4096               *:8082            *:*
```

Assume that the client stores the key as JSON in localStorage under key
`cloudgene`.

Azure API:

```sh
# B7 (§8.5) Egress to Azure, for task 2 §5. Expect 200 and 400 respectively
#           (a 400 from blob is fine — it proves the endpoint is reachable).
curl -sS -o /dev/null -w 'login: %{http_code}\n' -m 10 \
  https://login.microsoftonline.com/common/discovery/keys

# >>> login: 200

curl -sS -o /dev/null -w 'blob:  %{http_code}\n' -m 10 \
  https://daffstandard.blob.core.windows.net/

# >>> blob:  400
```

---

## Results

Fill these in and hand the whole file back. Redact any real email other than
your own, and never paste a token value.

### Part A — public API

Run 2026-09-14 against the public host, using two tokens for the `cameron`
account captured either side of a deliberate group-membership change: one
without `daff-wfs` (unentitled) and one with it (entitled). Both have since
expired.

| # | Check | Expected | Observed |
|---|---|---|---|
| A1 | §7.3 authorised body | `loggedIn: true`, an email, non-empty app ids | ✅ `loggedIn: true`, `mail: chyde@neoformit.com`, `apps: ["taxodactyl_150@1.5.0"]` (entitled token) |
| A2 | §7.5 corrupted signature | `loggedIn: false` | ✅ `loggedIn: false`, no `user` key |
| A3 | §7.5b tampered payload | `loggedIn: false` | ✅ `loggedIn: false`, no `user` key |
| A3b | forged token, `roles` incl. `daff-wfs`+`admin`, signed with wrong key | `loggedIn: false` | ✅ `loggedIn: false` — **signature is genuinely verified; claims cannot be forged** |
| A4 | §7.6 token header only | identical to A1 | ✅ identical; `X-Auth-Token` alone is sufficient, no `Referer`/`X-Requested-With`/cookie needed |
| A5 | §7.8 token after logout | unknown — this is the question | not yet run |
| A6 | §7.9 token renewed in-session | unknown | not yet run |
| A7 | §7.10 unentitled account | `loggedIn: true`, `apps: []` | ✅ **observed** — `loggedIn: true`, `apps: []` for a real session without the group. The rule denies. |
| A8 | §7.11 user can change own email | unknown | not yet run |
| A9 | `CLOUDGENE_USER_EMAIL` is email or username | unknown | not yet run |

**A7 resolved:** the `apps: []` response came from a genuinely unentitled
session — the operator removed the group membership deliberately to produce it.
So the negative case is **observed, not merely asserted**: a logged-in user
without the role gets `loggedIn: true` with `apps: []`, and the rule denies
them. A subsequent capture with membership restored returns
`apps: ["taxodactyl_150@1.5.0"]`. Both are now test fixtures.

### ⚠️ `apps` reflects live entitlement, not the token's `roles` claim

The unentitled token (`roles: ["user"]`, no `daff-wfs`) was replayed **after**
the operator restored the group membership. The same token string then returned
`apps: ["taxodactyl_150@1.5.0"]` — authorised — despite its own claims saying
otherwise.

Cloudgene therefore resolves entitlement from the account at request time and
ignores the `roles` baked into the token at issuance. Consequences:

- **Revocation is immediate and meaningful.** Remove someone's role and their
  existing, still-valid token stops authorising uploads on the next request —
  no waiting for the 24 h expiry. This is better than §4.2 of the task brief
  assumed, and it argues for keeping the validation cache TTL short or dropping
  it entirely (at 6.6 ms per call, see B4, it buys nothing).
- **The `roles` claim is stale in both directions** and must never be trusted.
  The spec already says never to parse the JWT for identity; this is the
  empirical proof of why.
- **A1/A7 are only meaningful as a pair with a timestamp.** The same token gave
  opposite answers 35 minutes apart because the account changed underneath it.

### Settled — the response carries three app lists, only one counts

`apps`, `deprecatedApps` and `experimentalApps` (the latter two empty in every
capture so far). **Decided by the operator, 2026-09-14: only `apps` counts.**
Entitlement to a deprecated or experimental app is *not* authorisation to
upload, so the rule stays `len(apps) > 0`.

The consequence to be aware of: a user whose only entitlement sits in
`deprecatedApps` or `experimentalApps` is denied, and the denial is deliberate.
If someone reports a `403` while insisting they can see a workflow in Cloudgene,
this is the first thing to check — the symptom looks identical to a bug.

An earlier draft recommended the opposite. That recommendation is withdrawn.

### Part B — shell on `cloudgene`

Completed by the operator, 2026-09-14.

| # | Check | Expected | Observed |
|---|---|---|---|
| B1 | §8.1 loopback with Host header | HTTP 200, JSON body | ✅ HTTP 200, full JSON body |
| B2 | §8.2 loopback without Host header | HTTP 200 | ✅ 200 — `Host` is not required |
| B3 | §8.2b loopback anonymous | HTTP 200, `loggedIn: false` | ✅ 200 — the startup self-test path works uncredentialed |
| B4 | §8.3 median loopback latency | well under 0.062 s | ✅ **0.0066 s** — ~10× faster than the public path |
| B5 | §8.4 files referencing `X-Auth-Token` | at least one hit | not run — operator confirms the `localStorage` contract by observation instead |
| B6 | §8.4b `localStorage` key used | `cloudgene`, value `{"token": ...}` | ✅ confirmed by observation (not from source) |
| B7 | §8.5 egress to Azure | `login: 200`, `blob: 400` | ✅ `login: 200`, `blob: 400` against `daffstandard.blob.core.windows.net` |
| B8 | §8.6 port 8003 free | no output / "port 8003 free" | ✅ free |
| B9 | ports in use | 8000, 8002, 8082 listening; 8003 absent | ⚠️ **only 8082 is listening** — 8000 (validation API) and 8002 (downloader) are not running |

**Decisions this settles:**

- **Validate over loopback.** `CLOUDGENE_BASE_URL=http://127.0.0.1:8082`, no
  `Host` header needed. At 6.6 ms a validation call per token issuance is
  negligible, so the ≤60 s identity cache is a nicety rather than a necessity —
  and dropping it would make logout revocation immediate.
- **Azure egress is open** from the Cloudgene host, and the storage account is
  `daffstandard` (task 2 §1 can be filled in).
- **Port 8003 is free** for the uploader.
- **B9 is worth a second look** — the nginx vhost proxies `/validation/api/` to
  8000, but nothing is listening there. Either that service is down or the
  config is stale. Unrelated to the uploader, but it is the kind of thing that
  is cheaper to notice now than to discover during a deploy.

### What each result decides

- **A1–A4** close out the §7 verification the agent could not finish without a
  token. A2/A3 are the load-bearing ones: if a tampered token is *accepted*,
  the whole forward-to-Cloudgene design is unsound and this stops being a
  verification exercise.
- **A5** sets the ceiling on the uploader's validation cache TTL and determines
  whether "revocation" means anything at all for us.
- **B1/B2/B3** decide whether the uploader validates over loopback (preferred:
  no TLS, no DNS, no dependency on the public vhost) or must call
  `https://cloudgene.qcif.edu.au`. The implementation defaults to the public
  host and switches with one environment variable, so either answer is cheap —
  but the answer must be recorded rather than assumed. If loopback rejects the
  request, record the status **and** the body; the reason matters.
- **B4** decides whether validating on every request is acceptable or whether
  the ≤60 s cache has to do real work.
- **B5/B6** confirm the `localStorage` coupling from Cloudgene's source, so §5
  of the spec rests on the code rather than on one browser observation.
- **B7/B8/B9** are deployment prerequisites, cheap to settle while a shell is
  already open.
