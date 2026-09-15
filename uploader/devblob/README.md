# Dev blob store

A local filesystem stand-in for Azure Blob Storage's write surface —
`comp=block` and `comp=blocklist` on `PUT`, nothing else — so that a
developer without Azure credentials can watch a real byte transfer, real
progress, real reconciliation and a real `az://` listing, all offline. See
[`../spec/tasks/06-mock-azure.md`](../spec/tasks/06-mock-azure.md) for the
design.

It is not in `uploader/api/`, and `uploader/api/**` must never import it
(enforced by a test in `uploader/api/tests/`). A router mounted inside
`app.py` behind a config flag is one refactor away from being reachable in
production; a module the production app never imports structurally cannot
ship. It also runs on its own origin, so the browser genuinely preflights
requests to it — exercising CORS the same way it will against
`*.blob.core.windows.net` — and its path shape stays `/{container}/{blob}`,
matching Azure's, rather than colliding with the API's own `/uploads`
prefix.

It is not an Azure emulator. Signatures are never verified, only expiry is
honoured, and nothing is served back out over `GET` — see §11 of the task
brief for the full list of what is deliberately out of scope.
