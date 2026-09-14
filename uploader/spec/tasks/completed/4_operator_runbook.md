# Task 4 — operator runbook: verify create-only SAS renewal against real Azure

This confirms the assumption renewal rests on (§2 "Create-only permission is
sufficient — but verify it" of
[`4_list_and_renew_endpoints.md`](4_list_and_renew_endpoints.md)): a
**second** create-only SAS, issued later for the same blob path, can commit
blocks staged under the **first** one. No code in this repo has made a real
Azure call, so this cannot be exercised in tests — it needs real Azure
credentials and a shell that can reach the storage account. The agent
cannot run any of it.

**Nothing here depends on the uploader service being deployed.** Deployment
is a separate, not-yet-performed step (see `api/README.md`'s "Deployment"
section). This test calls `azure_sas.AzureUserDelegationSasIssuer` directly
— the exact class `POST /uploads` and `POST /uploads/{id}/renew` use — from
a checkout of this repo, so it proves the SAS behaviour regardless of
whether the FastAPI process is running anywhere.

**This test only writes to Azure Blob Storage, not to `cloudgene`.** No SSH
to `cloudgene.qcif.edu.au` is used at any point — every request below goes
to `*.blob.core.windows.net`.

**Before you start:** `uploader/.env` already has everything `config.py`
needs (`UPLOADER_SAS_ISSUER`, `AZURE_STORAGE_ACCOUNT`,
`AZURE_STORAGE_CONTAINER`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
`AZURE_CLIENT_CERTIFICATE_PATH` — see `../azure.md`), and `config.py`'s
`load_config()` loads it automatically via `python-dotenv` — nothing to
source by hand. It is gitignored and never leaves your machine. Run this
from a machine with network access to Azure (your workstation is fine; it
does not need to be `cloudgene`), from wherever this checkout lives locally.

```sh
cd uploader/api
../venv/bin/pip install -r requirements.txt  # if not already done
```

## Step 1 — issue two independent SAS tokens for the same blob path

This is what `POST /uploads` followed by `POST /uploads/{id}/renew` does
internally: call `issuer.issue()` twice for the same `blob_path`, each a
fresh signature with its own expiry.

```sh
../venv/bin/python3 <<'PY'
from datetime import datetime, timedelta, timezone

from config import load_config
import azure_sas

config = load_config()
issuer = azure_sas.create_issuer(config)
assert isinstance(issuer, azure_sas.AzureUserDelegationSasIssuer), (
    "UPLOADER_SAS_ISSUER must be 'azure' for this test")

blob_path = "renewal-test/manual-check.txt"
expiry = datetime.now(timezone.utc) + timedelta(hours=1)

sas1 = issuer.issue(blob_path, expiry)
sas2 = issuer.issue(blob_path, expiry)

print(f"BLOB_PATH={blob_path}")
print(f"SAS1_URL={sas1.url}")
print(f"SAS2_URL={sas2.url}")
PY
```

Paste the three printed lines into your shell (or `eval` them) so
`$BLOB_PATH`, `$SAS1_URL` and `$SAS2_URL` are set for the steps below.

## Step 2 — stage one block with the first SAS

Block IDs must be base64 and the same length; `QUFBQUFBQUFBQUE=` and
`QUFBQUFBQUFBQUI=` below both encode to 12-byte strings.

```sh
BLOCK_A='QUFBQUFBQUFBQUE='   # base64("AAAAAAAAAAA")

curl -sS -X PUT "${SAS1_URL}&comp=block&blockid=${BLOCK_A}" \
  -H 'x-ms-blob-type: BlockBlob' \
  --data-binary 'hello block A' \
  -w '\nHTTP %{http_code}\n'
```

Expect `HTTP 201`. If this fails, stop — it means the SAS parameter
assembly itself is wrong (a Task 3 regression), not what this test checks.

## Step 3 — stage a second block with the second SAS

```sh
BLOCK_B='QUFBQUFBQUFBQUI='   # base64("AAAAAAAAAAB")

curl -sS -X PUT "${SAS2_URL}&comp=block&blockid=${BLOCK_B}" \
  -H 'x-ms-blob-type: BlockBlob' \
  --data-binary 'hello block B' \
  -w '\nHTTP %{http_code}\n'
```

Expect `HTTP 201`. This alone doesn't prove the point yet — staging under
SAS2 for a blob that doesn't exist is unsurprising. The real question is
Step 4.

## Step 4 — the load-bearing check: commit both blocks using SAS2

Uncommitted blocks belong to the *blob name*, not to the SAS that staged
them — this step is what actually tests that claim, by committing a block
staged under SAS1 using only SAS2's authority.

```sh
BODY="<?xml version=\"1.0\" encoding=\"utf-8\"?><BlockList><Latest>${BLOCK_A}</Latest><Latest>${BLOCK_B}</Latest></BlockList>"

curl -sS -X PUT "${SAS2_URL}&comp=blocklist" \
  -H 'Content-Type: application/xml' \
  --data-binary "$BODY" \
  -w '\nHTTP %{http_code}\n'
```

- **`HTTP 201`** — the assumption holds. Create-only is sufficient; the
  client work in [`../02-client.md`](../02-client.md) can proceed as
  designed.
- **`HTTP 403` (`AuthorizationPermissionMismatch` or similar)** — the
  assumption is wrong. **Do not widen the permission to `w` to make this
  pass** — that would let a SAS overwrite any blob it names, not just create
  one. Escalate back to the agent/spec instead: renewal may need to reuse
  the exact original SAS query string rather than mint a new one, or some
  other design change.

> RESULT: 201

## Step 5 — confirm and clean up

```sh
az storage blob show \
  --account-name "$AZURE_STORAGE_ACCOUNT" \
  --container-name "$AZURE_STORAGE_CONTAINER" \
  --name "$BLOB_PATH" \
  --auth-mode login \
  --query 'properties.contentLength'
# Expect 26 (len("hello block A") + len("hello block B"), 13 bytes each)
```

Delete the test blob when done — deletion isn't implemented by the API
(out of scope, §5), so this is a manual `az` step:

```sh
az storage blob delete \
  --account-name "$AZURE_STORAGE_ACCOUNT" \
  --container-name "$AZURE_STORAGE_CONTAINER" \
  --name "$BLOB_PATH" \
  --auth-mode login
```

## Result

| Check | Expected | Observed |
|---|---|---|
| Step 2: stage block A under SAS1 | `201` | ✅ `201` |
| Step 3: stage block B under SAS2 | `201` | ✅ `201` |
| Step 4: commit both blocks under SAS2 | `201` | ✅ `201` |
| Step 5: committed blob is 26 bytes | 26 | ✅ `26` |

Fill in and hand back. If Step 4 is not `201`, record the exact status code
and error body (`<Code>`/`<Message>` in the XML response) instead of just
pass/fail — the failure mode decides what changes.

## Once this passes

Nothing in the API code needs to change. This runbook exists to retire the
"unexercised" caveat on `AzureUserDelegationSasIssuer` (see its docstring in
`api/azure_sas.py`) and on the renewal design — record the result here and
that caveat can be removed in a follow-up edit.

> ALL PASSED
