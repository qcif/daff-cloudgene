# Task 8 — deleting an uploaded file

Let a user remove a file they uploaded. One route, one protocol method, one
button. The uploader is live on `cloudgene.qcif.edu.au` and uploads land in
the real container, so this is a change to a running service.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Anything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server.

Read [`../01-api.md`](../01-api.md) and [`../02-client.md`](../02-client.md)
first. Their settled decisions all still hold — in particular the
authorisation rule (`loggedIn` and a non-empty `apps`; `deprecatedApps` and
`experimentalApps` do **not** count), fail-closed error handling, and
owner-scoping every operation.

Deletion was listed as out of scope in §12 of
[`../02-client.md`](../02-client.md). This task supersedes that. The sibling
task [`09-az-path-in-file-list.md`](09-az-path-in-file-list.md) touches the
same component and the same list response; they can land in either order,
but see §5 below for the one line where they meet.

## 1. Why this is a backend route, not a wider SAS

The client cannot delete. Every SAS this service issues is `sp=c` — create
only, single blob — and that restriction is load-bearing: it is why a leaked
SAS cannot overwrite or destroy anything. Widening it to `d` to let the
browser delete would trade the strongest property the design has for a
convenience.

So the delete happens **server-side**, under the service principal, exactly
like blob property reads and listing already do. No Azure change is needed:
container-scoped `Storage Blob Data Contributor` already includes
`.../blobs/delete`. There is nothing for the operator to do in Azure for
this task, and no CORS change either — the browser never issues the delete
against storage, it calls this API same-origin.

## 2. `DELETE /files`

### Shape

```
DELETE /uploads/api/files
X-Auth-Token: <cloudgene jwt>
Content-Type: application/json

{"client_path": "reads_R1.fastq.gz"}
```

Not a path parameter. Blob paths contain `/` and an email address, so a path
parameter means double-encoding through nginx and a route that has to accept
`{path:path}` — a shape that invites a traversal bug for no benefit. A JSON
body on `DELETE` is well supported by Starlette and by `fetch`.

### The client sends the leaf, never the blob path

**`client_path` is the portion after the user's prefix, and the server
rebuilds the full path itself** with `naming.build_blob_path(email,
client_path)` — the same function `POST /uploads` uses.

This is the whole security design of the route. The caller never names a
blob path; it names a file *within its own prefix*, and the prefix is always
derived from the resolved identity. Every traversal defence in `naming.py`
(the three layers, the reserved segments, the Windows drive and control
character rules) therefore applies to deletion for free, and deleting
another user's blob is not "rejected", it is unexpressible.

Belt and braces anyway: assert the rebuilt path starts with
`naming.prefix_for(email)` before calling Azure, and raise rather than
delete if it does not. `build_blob_path` already guarantees this; the
assertion is there so a future refactor of `naming.py` fails a test instead
of leaking a delete.

Add `client_path` to each entry in the `GET /files` response so the client
has the value to send back and never has to derive it by string-slicing the
blob path. The two must come from the same source.

### Idempotent, like `/complete`

A blob that is already gone is not an error. Return `200` with

```json
{"deleted": true,  "blob_path": "...", "az_path": "az://uploads/..."}
{"deleted": false, "blob_path": "...", "az_path": "az://uploads/..."}
```

`false` meaning "nothing was there". A double-click, a retry after a flaky
response, or two tabs racing all converge on the same outcome — the same
rule §12 of the design spec applies to reconciliation. Do not use `404` for
this: the UI response to both cases is identical (drop the row, refresh the
list), and a `404` would make the client invent that equivalence itself.

### Refuse to delete a blob with a live `pending` record

If a matching record exists in state `pending` and has not expired, return
`409` with a message naming the situation: an upload to this path is in
progress; cancel or wait for it to finish, then delete.

The reason is that deletion does not revoke anything. The outstanding SAS
stays valid until `expires_at`, so an in-flight upload that is mid-transfer
will happily commit its block list *after* the delete and re-create the blob
— leaving the user staring at a file they just deleted. Better to say no.

An `expired`, `failed` or `completed` record imposes no such restriction.

### Leave the record table alone

Do not mutate, delete or add a state to the record on deletion. The records
are the issuance history — what was promised, and how it resolved — and the
container is the truth about what exists. `GET /files` already lists Azure
and annotates from records, so a deleted blob simply stops appearing. There
is no new state, and `TERMINAL_STATES` does not change.

The only trace is a log line, at the same level and shape as the issuance
line in `create_upload`:

```
deleted blob user=<email> blob=<blob path> existed=<true|false>
```

### Treat deletion as permanent

Nothing in [`../../azure.md`](../../azure.md) enables blob soft delete or
versioning on the storage account — they are only mentioned in passing, as
properties a careless ARM `PUT` would reset. **Assume there is no undelete**
until an operator confirms otherwise
(`az storage account blob-service-properties show --account-name ... --query
deleteRetentionPolicy`); that is the safe direction to be wrong in. Say so
in the confirmation UI (§4) and in
[`../../api/README.md`](../../api/README.md). If soft delete turns out to be
on, or is enabled later, this section is the one to revisit — the route does
not change, only what the user is told.

### Errors

Reuses the existing taxonomy exactly — no new codes:

| Code | When |
|---|---|
| `400` | `client_path` missing, or rejected by `naming.validate_client_path` |
| `401` | No token, or Cloudgene rejects it |
| `403` | No entitlement, or no email on the account |
| `409` | A live `pending` record exists for this path |
| `503` | Cloudgene or Azure unavailable, or the contract is broken |

No new rate limit. A delete costs one Azure call and can only ever touch the
caller's own prefix; the issuance limiter exists because a SAS is a
capability handed out, which this is not.

## 3. `BlobDeleter`, alongside `BlobLister`

Add `delete_blob(blob_path) -> bool` to `azure_sas.py` as its own protocol,
for the reason `BlobReader` and `BlobLister` are separate: signing,
reading, listing and deleting are four different capabilities, and a future
cleanup worker (the abandoned-blob sweep in
[`completed/2_azure_resources.md`](completed/2_azure_resources.md) §2.1)
needs delete without needing to sign.

Returns `True` if a blob was removed, `False` if it was not there — Azure's
`delete_blob` raises `ResourceNotFoundError`, which the adapter swallows
into `False` rather than letting a normal case travel as an exception.

Implement on all three issuers:

- `AzureUserDelegationSasIssuer` — `ContainerClient.delete_blob`, with
  `delete_snapshots="include"` so a future snapshot policy cannot leave the
  call failing on a blob that has them.
- `FakeSasIssuer` — pop from the in-memory blob table.
- `LocalFileSasIssuer` — `unlink()` the resolved path, through the **same**
  `_resolve()` that already contains the directory-escape check. Do not
  re-implement the path join. Prune the parent directory only if it is empty
  and inside the root.

`uploader/devblob` needs no change: the client still never talks to storage
for anything but `PUT`.

## 4. Client — the delete control

In `ExistingFiles.vue`, one destructive action per row.

- A trash / "Delete" button in the existing trailing column, `btn-sm` and
  danger-styled, disabled while a delete is in flight for that row.
- **Confirm before calling.** A native `confirm()` is sufficient and is what
  the rest of this page's plain-Bootstrap idiom implies — no modal component
  for one dialog. The prompt must contain the full `az://` path and the
  words "cannot be undone":

  ```
  Delete az://uploads/user@example.com/reads_R1.fastq.gz?
  This cannot be undone.
  ```

- On `200`, remove the row optimistically, then `loadExistingFiles()` so the
  table reflects Azure rather than a guess. `deleted: false` takes the same
  path — the file was already gone and the list was stale.
- On `409`, show the server's message verbatim (§5 of
  [`../02-client.md`](../02-client.md) — `409` is actionable) next to the
  row, and leave the row in place.
- Errors are per row, not page level. One failed delete must not clear the
  table or the upload results.

`api.js` gains `deleteFile(clientPath)` — `DELETE /files`, JSON body, no new
error handling; `request()` already maps every code this route can return.

The component currently takes only props. Deletion makes it interactive, so
it emits (`@deleted`) rather than calling the API itself, keeping `App.vue`
the only place that talks to `api.js`.

## 5. Where this meets task 9

The confirmation prompt above needs `f.az_path`, which the list response
already carries — so this task does **not** depend on
[`09-az-path-in-file-list.md`](09-az-path-in-file-list.md), which is about
what the *table cell* renders. Both touch `ExistingFiles.vue`. Whichever
lands second, re-read the other's section rather than assuming the file is
as its brief describes.

## 6. Tests

Backend, mirroring the style of `test_app.py`:

- A delete removes the blob and returns `deleted: true`; a second identical
  call returns `deleted: false` and still `200`.
- `client_path` values that `validate_client_path` refuses (`../`, a
  backslash, a colon, a control character, a leading `/`) all yield `400`
  and **no** call reaches the issuer — assert on the fake, not just on the
  status code.
- A delete naming a path that would resolve inside *another* user's prefix
  is impossible to express; the test that proves it is one where
  `client_path` is `"../bob@example.com/secret.csv"` and the result is a
  `400` with bob's blob still present.
- The two-users-with-a-shared-prefix case from task 4
  (`bob@example.com` vs `bob@example.com.au`) repeated for delete.
- A `pending`, unexpired record for the path gives `409`; the blob survives.
  An `expired` record for the same path gives `200`.
- Unauthenticated → `401`, unentitled → `403`, before any issuer call.
- `LocalFileSasIssuer.delete_blob` cannot escape its root.

Client, Vitest:

- `deleteFile` sends `DELETE` with the body and the `X-Auth-Token` header.
- A rejected `confirm()` makes no API call at all.
- A `409` leaves the row present and shows the message.

## 7. Documentation to update with the code

- [`../../api/README.md`](../../api/README.md) — the endpoint table, the
  `client_path` field on `GET /files`, and the permanence note from §2.
- [`../01-api.md`](../01-api.md) and [`../02-client.md`](../02-client.md)
  carry this design already; keep them true if the implementation diverges.
- [`../../azure.md`](../../azure.md) — a line under the role assignment
  noting that `Storage Blob Data Contributor` is now exercised for delete as
  well as read and list, so narrowing it would break this route.

## 8. Out of scope

Bulk or multi-select delete, an undo window, renaming, and any change to the
SAS permission set. Soft delete on the container is an operator decision,
not a code change, and is not requested here.
