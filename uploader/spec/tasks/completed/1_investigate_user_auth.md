# Task 1 — Cloudgene authentication: design and verification plan

**Status:** the design questions are answered. Cloudgene authenticates with an
**`X-Auth-Token` JWT header, not a session cookie** (§4), the token is readable
from same-origin `localStorage` under the key `cloudgene` (§5), and the
authorisation rule is settled: `loggedIn == true` **and** `apps` non-empty, with
`apps` confirmed to be entitlement-filtered (§3.1).

What remains is verification, split by who can do it: §7 runs over the public
HTTPS API and the agent does it directly; §8 requires a shell on the production
host and is therefore written as a runbook for the operator to run.

**Blocks:** §5 of [client-azure-upload.md](../client-azure-upload.md)

> ## ⛔ No agent shell access to production
>
> **The agent must not connect to the production server.** No `ssh cloudgene`,
> no `ssh ubuntu@cloudgene.qcif.edu.au`, no remote command execution by any
> other route, and no tooling that shells out to one. `cloudgene.qcif.edu.au` is
> a live service and is operated by people, not by agents.
>
> **HTTP requests to `https://cloudgene.qcif.edu.au` are permitted** — the agent
> may call the public API to test endpoints, as any client would. That covers
> all of §7. The prohibition is on shell access, not on using the service.
>
> If something can only be established from a shell, the agent's job is to
> **write the exact commands for the operator to run**, state what output to
> look for, and wait for the results to come back. See §8, which is structured
> for exactly that. Do not work around this restriction.
>
> Even over HTTP, stay read-only: `GET` requests to read state, never a call
> that submits a job, changes a setting, or writes anything.

## 1. The question

The uploader will be served from `https://cloudgene.qcif.edu.au/uploads/`. It
must turn whatever credential the browser holds into two facts:

1. **Who is this?** — specifically the user's email address, which becomes their
   blob prefix.
2. **Are they allowed?** — do they have access to any workflow?

The approach: **rather than reading Cloudgene's database, the uploader forwards
the client's token to Cloudgene and infers both facts from the response.** §4
confirms the endpoint (`/api/v2/server`) and the decision rule.

> **Correction to the spec.** [client-azure-upload.md](../client-azure-upload.md)
> §5–§6 originally claimed that same-origin placement means "the browser sends
> the Cloudgene session cookie automatically". That is **wrong**. The §4 capture
> carries only a `consent-policy` cookie — a cookie-consent banner artefact,
> nothing to do with auth. The credential is the `X-Auth-Token` header, and
> **nothing is sent automatically**: the uploader's frontend must acquire the
> token itself and attach it to every API call.
>
> Same-origin placement still matters, but for a different reason — it is what
> lets the uploader's JS read the token out of Cloudgene's `localStorage` (§5).
> The spec has been updated accordingly.

## 2. Ground rules

- **No production access by the agent.** See the box above. This is absolute.
- **Nothing is modified anywhere.** The verification in §7 and §8 is read-only:
  no config changes, no service restarts, no database writes, no package
  installs. If a check would require changing something, it does not get run —
  it gets raised.
- **Redact credentials.** Real token values, the JWT signing secret, and user
  emails other than the operator's own must be redacted in anything written to
  this repo.
- **Ask rather than guess.** If a fact cannot be established from the material
  here, the answer is a question for the operator, not an assumption.

## 3. The test credential

The operator supplies this — log into `https://cloudgene.qcif.edu.au`, open the
devtools Network tab, right-click an `/api/v2/...` XHR → Copy as cURL. The value
of interest is the `X-Auth-Token` header. Tokens last 24 hours (§4.2), so
captures go stale fast; request a fresh one rather than working around an
expired token.

### 3.1 The authorisation rule — SETTLED

**Access to any workflow *is* the authorisation** (confirmed by the operator).
Untrusted users are never granted access to a workflow, so a user who can run
one has already been approved and may upload.

```
authorised  ==  loggedIn == true  AND  len(apps) > 0
```

No per-app matching, no role list to keep in sync with Cloudgene, and nothing
that breaks when a workflow is version-bumped.

The assumption this rests on is **confirmed**: `apps` is filtered by
entitlement, and a logged-in user without the appropriate role receives an empty
list. So the three cases collapse cleanly — approved users get a populated list,
logged-in-but-unentitled users get `[]`, and anonymous callers get `[]` with
`loggedIn: false`.

## 4. The authorisation call

An authenticated user will get the following response from
`https://cloudgene.qcif.edu.au/api/v2/server`:

```json
// Authorized user
{
    "user": {
        "username": "usernameXYZ",
        "mail": "user@example.com",
        "admin": false,
        "name": "Joe Bloggs"
    },
    "apps": [
        {
            "id": "taxodactyl_xxx@1.2.3",
            "name": "Taxodactyl",
            "version": "1.2.3"
        }
    ],
    "loggedIn": true,
    ...
}

// Anonymous user
{
    "apps": [],
    "loggedIn": false,
    ...
}
```


The criteria for an authorized users is both of the following:

- `loggedIn: true`
- `apps` list is not empty

An example curl equivalent of this request (copied from chrome devtools):

```sh
curl --url 'https://cloudgene.qcif.edu.au/api/v2/server' \
  -H 'Accept: application/json, text/javascript, */*; q=0.01' \
  -H 'Accept-Language: en-GB,en-US;q=0.9,en;q=0.8' \
  -H 'Cache-Control: no-cache' \
  -H 'Connection: keep-alive' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -b 'consent-policy=%7B%22ess%22%3A1%2C%22func%22%3A1%2C%22anl%22%3A1%2C%22adv%22%3A1%2C%22dt3%22%3A1%2C%22ts%22%3A29468392%7D' \
  -H 'Pragma: no-cache' \
  -H 'Referer: https://cloudgene.qcif.edu.au/' \
  -H 'Sec-Fetch-Dest: empty' \
  -H 'Sec-Fetch-Mode: cors' \
  -H 'Sec-Fetch-Site: same-origin' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36' \
  -H 'X-Auth-Token: xxxxx' \
  -H 'X-CSRF-Token: undefined' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -H 'sec-ch-ua: "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Linux"'
```

Though the request payload in Chrome does include a cookie header:

```
accept:  application/json, text/javascript, */*; q=0.01
accept-encoding: gzip, deflate, br, zstd
accept-language: en-GB,en-US;q=0.9,en;q=0.8
cache-control: no-cache
connection:  keep-alive
content-type:  application/x-www-form-urlencoded
cookie:  consent-policy=%7B%22ess%22%3A1%2C%22func%22%3A1%2C%22anl%22%3A1%2C%22adv%22%3A1%2C%22dt3%22%3A1%2C%22ts%22%3A29468392%7D
host:  cloudgene.qcif.edu.au
pragma:  no-cache
referer: https://cloudgene.qcif.edu.au/
sec-ch-ua: "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"
sec-ch-ua-mobile:  ?0
sec-ch-ua-platform:  "Linux"
sec-fetch-dest:  empty
sec-fetch-mode:  cors
sec-fetch-site:  same-origin
user-agent:  Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36
x-auth-token:  eyJhbGciO****
x-csrf-token:  undefined
x-requested-with:  XMLHttpRequest
```

> The token above is truncated — the original capture was a live token for the
> `cameron` account, valid until 2026-09-15T00:35Z. Its decoded claims are
> reproduced in §4.2. Never commit a complete token.

### 4.1 The cookie is not the credential

The only cookie present is `consent-policy`, which decodes to a cookie-consent
banner's preferences (`{"ess":1,"func":1,"anl":1,"adv":1,"dt3":1,...}`). There
is no session cookie. **Authentication is entirely `X-Auth-Token`** — the single
most consequential finding for this design.

One useful side effect: because the credential is a custom header rather than an
ambient cookie, the uploader's API is inherently CSRF-resistant — a cross-site
page cannot make an authenticated request to it. (Consistent with
`X-CSRF-Token: undefined` being accepted; Cloudgene does not appear to enforce
CSRF on this endpoint, because it does not need to.)

### 4.2 The token is a JWT — decoded

```json
// header
{ "alg": "HS256" }

// payload
{
  "sub": "cameron",
  "roles": ["user", "daff-wfs"],
  "iss": "cloudgene",
  "iat": 1789346124,   // 2026-09-14T00:35:24Z
  "nbf": 1789346124,
  "exp": 1789432524    // 2026-09-15T00:35:24Z  (24h lifetime)
}
```

This tells us a great deal:

- **`roles` is the group membership.** `daff-wfs` is presumably the role
  granting workflow access, and `user` the baseline every account gets. The
  uploader does **not** authorise on this claim — it is inside a token the
  uploader never verifies (§6), and the policy is entitlement-based rather than
  role-based anyway (§3.1). It is valuable as corroboration, and as the likely
  input to the `apps` filter.
- **`sub` is the username, not the email.** The blob prefix therefore cannot
  come from the token alone — it needs `user.mail` from the `/api/v2/server`
  response.
- **`HS256`** means Cloudgene signs with a symmetric secret held server-side.
  Whoever holds that secret can both verify *and mint* tokens, so it is a
  high-value credential (see §6).
- **24-hour lifetime, no `jti`.** There is no per-token identifier, which
  suggests logout may be purely client-side — the frontend discards the token
  while it stays valid server-side until `exp`. §7 tests this. If true it is a
  real finding about Cloudgene, and it bounds what "revocation" can possibly
  mean for us.

### 4.3 Settled points about the response

- **"`apps` non-empty" is a sound authorisation test.** The list is
  entitlement-filtered, empty for anonymous callers and empty for logged-in
  users without the role (§3.1).
- **App ids are informational, not a filter.** The uploader does not match on
  app id, so no pattern needs configuring. The ids are still worth logging (the
  capture shows `taxodactyl_xxx@1.2.3`, id and version joined by `@`), and if
  the rule ever needs narrowing, the versioned ids (`taxodactyl`,
  `taxodactyl_144`, `taxodactyl_150`, `nanopore-assembly`, …) are what it would
  have to cope with.
- **No roles in the response.** `/api/v2/server` returns identity (`user`) and
  entitlement (`apps`) but *not* the role list, so authorisation is derived from
  `apps`; there is no `roles` field to check directly.

## 5. How the uploader gets the token — ANSWERED

Cloudgene persists the token in `localStorage` on
`https://cloudgene.qcif.edu.au`, under the key **`cloudgene`**, holding a JSON
object:

```json
{ "token": "eyJhbGci****" }
```

Because `localStorage` is scoped to the **origin**, not the path, the uploader's
app at `/uploads/` reads exactly the same store as Cloudgene at `/`. No
hand-off, no fragment passing, no second login. This is the good outcome, and it
is what makes same-origin sub-path deployment (§6 of the spec) a requirement
rather than a convenience.

The frontend therefore does:

```js
const AUTH_STORAGE_KEY = 'cloudgene';

function getAuthToken() {
    try {
        const raw = window.localStorage.getItem(AUTH_STORAGE_KEY);
        return raw ? (JSON.parse(raw).token || null) : null;
    } catch (e) {
        return null;   // absent or unparseable -> treat as logged out
    }
}
```

and attaches the result as `X-Auth-Token` on every call to the uploader's API.
No token, or a token the backend rejects, means redirect to the Cloudgene login
with a return URL — never a partially functional page.

### 5.1 Fallbacks — recorded, not expected to be needed

Retained only in case the verification turns up something that invalidates the
above:

- **Entry via Cloudgene**, passing the token in the URL fragment
  (`#token=...`). Fragments are never sent to servers and stay out of access
  logs, but they do land in browser history.
- **A short-lived hand-off token** minted by Cloudgene for this purpose.
  Cleaner, but requires a Cloudgene-side change: much larger work.
- **The uploader does its own login.** Worst option — a second login is exactly
  what this design set out to avoid — but it is the floor.

## 6. Validation strategy: call Cloudgene, or verify the JWT locally?

| | **A. Forward to `/api/v2/server`** | **B. Verify the JWT locally** |
|---|---|---|
| How | Replay the token; read `loggedIn`, `apps`, `user.mail` | Verify the HS256 signature with Cloudgene's secret; read `roles` |
| Email | Returned as `user.mail` | **Not in the token** — still needs a lookup |
| Latency | One request per validation | Microseconds, no network |
| Depends on Cloudgene running | Yes | No |
| Sees logout / disabled accounts | Yes, if Cloudgene tracks it at all | No — valid until `exp` regardless |
| Needs the signing secret | No | **Yes** — a mint-anything credential on our side |

**Decision: A.** It needs no secret, returns the email we require, and defers all
policy to Cloudgene. B's only advantage is latency, and it buys that by copying
a secret that can forge any user's identity into a second service — a poor trade
for an app doing a handful of token issuances per user per day.

B is therefore **not** to be pursued, and the uploader must never be given the
Cloudgene signing secret. Revisit only if §8's latency measurement shows a
genuine problem, and even then prefer caching the verdict over holding the key.

## 7. Verification over HTTPS — the agent runs this

None of this needs a shell: it goes to the public endpoint, so **the agent runs
§7.1–§7.7 itself** once the operator supplies a token (§3). All `GET`s, all
read-only. §7.8–§7.11 need a browser or a second account and stay with the
operator.

Set up once:

```sh
CG=https://cloudgene.qcif.edu.au/api/v2/server
TOKEN='<paste the X-Auth-Token value>'
```

| # | Command | What to record |
|---|---|---|
| 7.1 | `curl -sS -o /dev/null -w '%{http_code}\n' "$CG"` | Status with **no** token. Expected `200`. |
| 7.2 | `curl -sS "$CG" \| jq '{loggedIn, apps}'` | Anonymous body. Expected `loggedIn: false`, `apps: []`. |
| 7.3 | `curl -sS "$CG" -H "X-Auth-Token: $TOKEN" \| jq '{loggedIn, mail: .user.mail, apps: [.apps[].id]}'` | Authorised body. Expected `loggedIn: true`, a real email, non-empty ids. |
| 7.4 | `curl -sS "$CG" -H 'X-Auth-Token: not-a-token' \| jq '{loggedIn, apps}'` | Garbage token. Expected `loggedIn: false`. |
| 7.5 | `curl -sS "$CG" -H "X-Auth-Token: ${TOKEN}x" \| jq '{loggedIn, apps}'` | Token with a corrupted signature. Expected `loggedIn: false` — this is what proves the signature is actually verified. |
| 7.6 | `curl -sS "$CG" -H "X-Auth-Token: $TOKEN"` with **only** that header | Confirms `X-Requested-With`, `Referer` and the rest are not required. |

**7.7 — minimum headers.** 7.6 already answers this if it succeeds. If it fails,
add headers back one at a time until it works and record which one mattered.

### Operator-only (needs a browser or a second account)

**7.8 — logout behaviour.** Copy a working token, log out of Cloudgene in the
browser, then re-run 7.3 with the copied token. Does it still work? This is the
single most useful operational answer: it determines whether revocation is
meaningful and what the cache TTL ceiling should be.

**7.9 — token renewal.** Leave a logged-in tab open, and check whether the
`cloudgene` key in `localStorage` changes value before the 24h expiry. Sets the
frontend's expectations for a long-lived page.

**7.10 — an unentitled account, if one exists.** Run 7.3 with that user's token.
Expected `loggedIn: true`, `apps: []`. This is the only check that demonstrates
the rule actually *denies* somebody; the operator has confirmed the behaviour,
so this is corroboration rather than discovery. Skip if no such account exists —
do not create one on production for this.

**7.11 — email semantics.** From the Cloudgene UI: can a user change their own
email? If so, their blob prefix moves with it and earlier uploads are stranded
under the old prefix. Worth knowing even if we accept it.

## 8. Verification that requires a shell — runbook for the operator

**The agent does not run any of this.** It is written to be pasted into a shell
on `cloudgene` by the operator, who pastes the output back. Every command is
read-only: no writes, no restarts, no installs.

```sh
# 8.1 Does Cloudgene answer on loopback, bypassing nginx?
#     Expect HTTP 200 and a JSON body.
TOKEN='<paste token>'
curl -sS -i -m 5 http://127.0.0.1:8082/api/v2/server \
  -H 'Host: cloudgene.qcif.edu.au' \
  -H "X-Auth-Token: $TOKEN" | head -20

# 8.2 Same, without the Host header — does Cloudgene care?
curl -sS -o /dev/null -w '%{http_code}\n' -m 5 \
  http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"

# 8.3 Latency over 20 sequential loopback calls (median is what matters).
for i in $(seq 20); do
  curl -sS -o /dev/null -w '%{time_total}\n' \
    http://127.0.0.1:8082/api/v2/server -H "X-Auth-Token: $TOKEN"
done | sort -n | awk '{a[NR]=$1} END {print "median", a[int(NR/2)+1]}'

# 8.4 Where does the frontend put the token? Confirms the localStorage
#     contract in §5 at the source, rather than by observation.
grep -rl 'X-Auth-Token' /mnt/data/cloudgene 2>/dev/null | head
#     ... and if the frontend is inside the jar:
#     unzip -l /mnt/data/cloudgene/*.jar | grep -iE '\.js$' | head

# 8.5 Egress to Azure, for task 2 §5. Expect HTTP 200 and 400 respectively
#     (400 from blob is fine — it proves the endpoint is reachable).
curl -sS -o /dev/null -w 'login: %{http_code}\n' -m 10 \
  https://login.microsoftonline.com/common/discovery/keys
curl -sS -o /dev/null -w 'blob:  %{http_code}\n' -m 10 \
  https://<account>.blob.core.windows.net/

# 8.6 Is port 8003 free for the uploader? Expect no output.
ss -lntp 2>/dev/null | grep ':8003' || echo 'port 8003 free'
```

What the results decide:

- **8.1/8.2** — whether the uploader can use loopback (preferred: lower latency,
  no dependency on external DNS or TLS) or must go via
  `https://cloudgene.qcif.edu.au`. If loopback rejects the request, record the
  status and body, because the reason matters.
- **8.3** — whether validating on every request is acceptable, or whether the
  ≤60s cache in the spec needs to do more work.
- **8.4** — confirms the `localStorage` key and shape from Cloudgene's own
  source, making the §5 coupling predictable rather than observed.
- **8.5/8.6** — prerequisites for deployment, cheap to check while a shell is
  already open.

## 9. Identity — settled, with one open question

§4 shows the email arrives as `user.mail`; §4.2 shows `sub` is the *username*,
so the token alone is insufficient. The blob prefix comes from `user.mail`.

Open, and answerable from the repo rather than the server: which of username and
email does Cloudgene pass to workflows as `CLOUDGENE_USER_EMAIL`? The
[workflows/](../../../workflows/) definitions use that variable, and the
uploader's blob prefix should match it so that a user's uploads and their job
outputs agree on who they are.

## 10. Deliverables

1. **§7.1–§7.7 run and recorded** by the agent against the public API, with the
   actual responses (redacted) in the report.
2. **A shell runbook** — §8 plus §7.8–§7.11, refined into something the operator
   can paste and return results from in one pass, with an empty results table to
   fill in. This is what the agent produces *instead of* touching the server.
3. **A report** at `uploader/spec/tasks/1_investigate_user_auth_report.md`: the
   §7 results, the exact request the uploader should make, and how to interpret
   every possible response. Completed once the operator returns the §8 results.
4. **The validation implementation**, written against the confirmed contract:

   ```python
   def validate_token(auth_token: str) -> tuple[str, bool]:
       """Return (email, is_authorised) for a Cloudgene X-Auth-Token.

       Raises on any failure to reach or interpret Cloudgene — callers must
       fail closed, never default-allow.
       """
   ```

   Unit-tested against fixtures recorded from §7, and smoke-tested against the
   public endpoint. Only the loopback base URL waits on §8.1.

## 11. Risks to carry into the implementation

- **Confused deputy.** The uploader asks Cloudgene "is the bearer of this token
  allowed?" and acts on the answer. It must forward *only* the token and trust
  *nothing* else from the client — no email, username, role or app id from the
  request body is ever used, even though a JWT payload is trivially readable
  without the key. **Never parse the JWT for identity without verifying it.**
- **A JS-readable token is XSS-exposed.** The token sits in `localStorage` (§5),
  so any script running on the origin can read it. An accepted inherited risk —
  it is Cloudgene's existing posture, and the uploader reading the same token
  adds no new exposure. It does mean the uploader's own frontend must not
  introduce an XSS vector (no `v-html` on anything derived from a filename or a
  blob listing), since that would widen an existing weakness.
- **Reading another app's private storage is a coupling point.** The key name
  `cloudgene` and its `{"token": ...}` shape are internal to Cloudgene and could
  change on upgrade. The failure must be "no token found → redirect to login",
  which is safe; §8.4 confirms the shape at the source.
- **Don't "fix" this by moving to cookie auth.** Header-based credentials make
  the uploader's API CSRF-resistant for free (§4.1). Preserve that.
- **The signing secret stays in Cloudgene.** Per §6, the uploader never holds a
  credential capable of minting identities.
- **Coupling to an internal endpoint.** `/api/v2/server` is a frontend-facing
  API, not a documented integration point; an upgrade can change its shape
  silently. A startup self-test asserting the expected response shape is cheap,
  and must fail closed rather than degrade to allow-all.
- **Availability.** This puts Cloudgene in the uploader's request path —
  acceptable, since the user could not have reached the uploader otherwise — but
  the failure must be a clean `503`.
