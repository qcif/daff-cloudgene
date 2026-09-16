# Task 1 — Cloudgene authentication: verification report

**Date of observations:** 2026-09-14 (all HTTPS checks run from an external
network against `https://cloudgene.qcif.edu.au`).

**Status:** partially complete. The anonymous and invalid-token cases are
**verified and match the brief**. Everything needing a live `X-Auth-Token` or a
shell on the production host is **outstanding**, and is set out in
[1_operator_runbook.md](./1_operator_runbook.md).

The implementation (deliverable 4) is written, linted and unit-tested, and can
ship as soon as §8.1 settles which base URL it points at. It defaults to the
public host and switches with one environment variable, so it is not blocked.

## How to read this document

Claims are tagged so that nothing inferred is mistaken for something seen:

- **OBSERVED** — the agent made the request and saw this.
- **ASSERTED** — stated as already-confirmed in the task brief or by the
  operator; the agent did not independently verify it.
- **INFERRED** — a conclusion drawn from observations, not itself observed.
- **UNVERIFIED** — could not be run; no data.

No real token, secret or third-party email appears in this document or in the
test fixtures.

---

## 1. What was observed

### 1.1 §7.1 — anonymous request, status code — OBSERVED

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://cloudgene.qcif.edu.au/api/v2/server
```

```
200
```

Response headers: `HTTP/1.1 200 OK`, `Server: nginx/1.24.0 (Ubuntu)`,
`Content-Type: application/json`, `Content-Length: 1902`. No `Set-Cookie`, no
`WWW-Authenticate`.

**The brief is correct.** An unauthenticated caller gets `200`, not `401`.

### 1.2 §7.2 — anonymous body — OBSERVED

```json
{
  "apps": [],
  "loggedIn": false,
  "name": "DAFF Biosecurity workflows",
  "emailRequired": true,
  "oauth": [],
  "maintenace": false,
  "navigation": [ ... ],
  "background": "...", "foreground": "...", "footer": "...",
  "userEmailDescription": "...", "userWithoutEmailDescription": "..."
}
```

Full top-level key set, verbatim:

```
apps, background, emailRequired, footer, foreground, loggedIn, maintenace,
name, navigation, oauth, userEmailDescription, userWithoutEmailDescription
```

**The brief is correct on both counts it asserts** — `loggedIn: false` and
`apps: []`.

**One thing the brief does not mention, and the implementation depends on:
there is no `user` key at all in the anonymous response.** The brief's §4
example shows the anonymous body with `apps` and `loggedIn` and an elided
`...`, which leaves it ambiguous whether `user` is present-but-empty or absent.
It is absent. Code that reaches for `data["user"]["mail"]` before checking
`loggedIn` raises `KeyError` on every anonymous request, so the order of checks
in the validator is load-bearing rather than stylistic.

The complete body is committed as a test fixture at
`uploader/api/tests/fixtures/anonymous.json`. It contains no credentials —
it is what any anonymous caller receives.

Note also the typo in Cloudgene's own field name: `maintenace`, not
`maintenance`. Worth knowing before anything is built on it.

### 1.3 §7.4 — garbage token — OBSERVED

```sh
curl -sS https://cloudgene.qcif.edu.au/api/v2/server \
  -H 'X-Auth-Token: not-a-token'
```

`HTTP 200`, body **byte-identical to the anonymous response**: `loggedIn:
false`, `apps: []`, no `user` key. A malformed credential is treated exactly as
no credential — no error, no distinguishing signal.

This check needed no live token, so it was run despite §7.3–§7.7 otherwise
being blocked.

### 1.4 §7.5, approximately — forged JWT — OBSERVED, with a caveat

The agent minted a **synthetic** JWT of its own: correct three-segment
structure, `{"alg":"HS256"}` header, a plausible Cloudgene payload
(`sub`, `roles: ["user","daff-wfs"]`, `iss: "cloudgene"`, unexpired `exp`), and
a signature segment of filler. No real token was used or reconstructed.

`HTTP 200`, `loggedIn: false`, `apps: []`, no `user` key.

**INFERRED, not observed:** Cloudgene verifies the signature rather than merely
decoding the token. The evidence is one-sided — it shows a token with an
invalid signature being rejected, which is also consistent with rejection for
some other reason (unknown `sub`, a session lookup, anything). The decisive
test is §7.5 proper: take a token Cloudgene *does* accept and corrupt only its
signature. That needs a live token and is A2/A3 in the runbook.

### 1.5 Header minimalism — OBSERVED for the anonymous path only

Every request above was plain `curl` — no `Referer`, no `X-Requested-With`, no
`X-CSRF-Token`, no cookie, no browser `User-Agent`. All succeeded. So the
endpoint does not require the browser-ish headers that appear in the devtools
capture in §4 of the brief, at least when unauthenticated.

**This does not close §7.6.** It remains possible, if unlikely, that the
*authenticated* path applies a check the anonymous path does not. A4 in the
runbook settles it.

### 1.6 Latency baseline — OBSERVED

Ten sequential public-HTTPS `GET`s from an external network:

```
median 0.0615 s   min 0.0537 s   max 0.1041 s
```

This is the upper bound; loopback should be materially faster and is measured
by B4. **INFERRED:** even at 62 ms, per-request validation is affordable for an
app issuing a handful of tokens per user per day, so the ≤60 s cache in §5.3 of
the spec is an optimisation rather than a necessity. Confirm against B4 before
relying on it.

---

## 2. What is blocked, and on what

### 2.1 Blocked on a live `X-Auth-Token`

The agent was given no credential. The token quoted in §4 of the brief is
truncated and expired; it was not used, not reconstructed, and not attacked.

| Check | What it establishes | Runbook |
|---|---|---|
| §7.3 authorised body | that `user.mail` and `apps` appear as §4 claims | A1 |
| §7.5 corrupted signature | that the signature is genuinely verified | A2/A3 |
| §7.6/§7.7 minimum headers | that only `X-Auth-Token` is required | A4 |

**No result is reported for any of these. They were not run.**

A2/A3 carry the most weight. §1.4 above is suggestive but not conclusive, and
if a tampered token were ever *accepted*, the entire forward-to-Cloudgene
design would be unsound — this is the check that must not be quietly skipped.

### 2.2 Blocked on the operator's shell or browser

Per the standing prohibition, the agent did not connect to the production host
by any route. Outstanding: §7.8 (logout behaviour), §7.9 (token renewal), §7.10
(an unentitled account), §7.11 (email mutability), and all of §8 — loopback
reachability, loopback latency, the `localStorage` contract in Cloudgene's own
source, Azure egress, and port 8003.

All of it is written up as copy-pasteable read-only commands with a blank
results table in [1_operator_runbook.md](./1_operator_runbook.md).

§7.8 is the one worth pushing on. Whether a token survives a logout decides
whether revocation means anything for us, and sets the ceiling on the
validation cache TTL.

### 2.3 Not blocked, just not in this task's scope

§9's open question — whether `CLOUDGENE_USER_EMAIL` carries the email or the
username — is answerable from the repo. It is included as A9 in the runbook
because it wants an operator's knowledge of the deployment rather than a grep,
and because the uploader's blob prefix should agree with whatever job outputs
already use.

---

## 3. The exact request the uploader makes

```
GET {base}/api/v2/server
X-Auth-Token: <the client's token, forwarded byte-for-byte>
Accept: application/json
```

- `{base}` is `https://cloudgene.qcif.edu.au` today, and becomes
  `http://127.0.0.1:8082` once B1/B2 confirm loopback. One environment
  variable, `CLOUDGENE_BASE_URL`.
- **No other header is sent**, and no cookie. OBSERVED in §1.5 that the
  anonymous path needs nothing else; A4 confirms the same for the
  authenticated path.
- **Nothing from the client is forwarded except the token.** Not the request
  body, not a client-supplied email, not the JWT's decoded claims. The JWT is
  never parsed for identity — its payload is readable without the key and is
  therefore untrusted input (§11 of the brief, §5.3 of the spec).
- Timeout 5 s. A slow Cloudgene is a `503`, not a hang.
- The token is validated as *sendable* before the call — non-empty, no control
  characters, not absurdly long. A client with no token costs no round trip,
  and a token containing CRLF is refused rather than spliced into the header.

## 4. How to interpret every possible response

| Cloudgene's answer | Meaning | Uploader's response |
|---|---|---|
| 200, `loggedIn: true`, `apps` non-empty, `user.mail` present | Authenticated and authorised | Proceed. Blob prefix = `user.mail`, lowercased |
| 200, `loggedIn: true`, `apps: []`, `user.mail` present | Authenticated, entitled to no workflow | **403** with an explanation. No SAS |
| 200, `loggedIn: false` | No token, expired, forged, or logged out — OBSERVED to be the answer for absent, garbage and forged tokens alike | **401**. Client redirects to the Cloudgene login |
| 200, `loggedIn: true`, but no `user` object or no `user.mail` | Contract broken — the endpoint changed | **503**, log loudly. Never fall back to another identity source |
| 200, `loggedIn` absent | Contract broken | **503** |
| 200, `loggedIn` present but not a boolean (e.g. `"true"`) | Contract broken. Note `"true"` is truthy in Python — this is a real trap | **503** |
| 200, `apps` absent or not a list | Contract broken | **503** |
| Any non-200 status | Not a documented behaviour of this endpoint | **503** |
| Body is not JSON (nginx error page, maintenance splash) | Cloudgene down or fronted by an error page | **503** |
| Connection refused, timeout, DNS failure | Cloudgene unreachable | **503** |

The rule that makes all of this safe: **a `200` is never evidence of
authentication.** OBSERVED — absent, garbage and forged tokens all return `200`
with an ordinary-looking body. Only the parsed `loggedIn` boolean means
anything, and every deviation from the expected shape is a failure, never a
degradation to allow.

Authorisation, unchanged from §3.1 of the brief (ASSERTED by the operator, not
independently verified — A7 corroborates it):

```
authorised  ==  loggedIn is True  AND  len(apps) > 0
```

## 5. Deliverable 4 — the implementation

`uploader/api/cloudgene_auth.py`, with tests in
`uploader/api/tests/test_cloudgene_auth.py` and the recorded fixture in
`uploader/api/tests/fixtures/anonymous.json`.

```python
def validate_token(auth_token: str) -> tuple[str, bool]:
    """Return (email, is_authorised) for a Cloudgene X-Auth-Token."""
```

- `interpret_server_info(data)` holds the decision logic and is separated from
  the HTTP call, so it is testable against recorded bodies without a network.
- Exceptions map straight onto status codes: `NotAuthenticatedError` → 401,
  `CloudgeneUnavailableError` → 503, `CloudgeneContractError` → 503. All
  inherit `CloudgeneAuthError`, so a caller that catches only the base class
  still fails closed — **no code path returns a value on error.**
- `is_authorised is False` is returned for exactly one case: a genuinely
  logged-in user entitled to no workflow. Everything else raises.
- `startup_self_test()` implements the self-test §5.3 of the spec asks for. It
  uses an **anonymous** request, so it needs no credential, and asserts the
  anonymous contract observed in §1.2: HTTP 200, `loggedIn: false`, `apps: []`.
- `CLOUDGENE_BASE_URL` selects the host, defaulting to the public one. **Switch
  to `http://127.0.0.1:8082` once B1/B2 come back.**

33 unit tests, all passing; `flake8` clean. The fixture-backed tests use the
real §7.1/§7.2/§7.4 body. The authorised and unentitled bodies are
**synthetic** — modelled on §4 of the brief and clearly labelled as such in the
test module — and must be re-checked once A1 returns a real response.

Smoke-tested against the live public endpoint: `startup_self_test()` passes,
and `validate_token('not-a-token')` raises `NotAuthenticatedError`.

## 6. Corrections to the brief

The brief's §7.1 and §7.2 assertions are **confirmed**, exactly as written.
Two things to add rather than correct:

1. **The anonymous response contains no `user` key.** §4's `...` elision hides
   this. It dictates the order of checks in any validator.
2. **§7.4 does not need a token** and has been run: a garbage token is
   indistinguishable from no token, byte for byte.

## 7. What is needed to finish

1. A fresh `X-Auth-Token`, or the operator running Part A of the runbook —
   closes §7.3, §7.5, §7.6, §7.7 and replaces the synthetic fixtures with real
   ones.
2. Part B of the runbook — closes §8. **B1/B2 is the one that gates a code
   change**, and it is a one-line configuration change either way.
3. The §7.8 answer specifically, to set the cache TTL ceiling.

Until 1 and 2 come back, this report is accurate but incomplete, and it says so
in every place where it is.
