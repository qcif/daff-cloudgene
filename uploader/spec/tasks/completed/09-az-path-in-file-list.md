# Task 9 — show the full `az://` path in "Your files in storage"

A small, self-contained frontend change: the existing-files table shows the
full `az://` path for each file, and lets the user copy one. No backend
change — the field has been in the list response since task 4.

> ⛔ **No SSH to `cloudgene.qcif.edu.au`.** Read-only `GET` requests to the
> public host are permitted. Anything requiring a shell is written up as
> commands for the operator. Do not deploy, restart, or edit anything on the
> server.

Read §8 of [`../02-client.md`](../02-client.md) first. It already settles the
rule this task extends to a second place in the UI.

## 1. What is wrong today

[`../../client/src/components/ExistingFiles.vue`](../../client/src/components/ExistingFiles.vue)
renders `f.blob_path` in the Path column, truncated at `20rem`:

```
user@example.com/reads_R1.fastq.gz
```

That is an internal identifier. The value a user actually needs is the one
they paste into a workflow, and that is the `az://` path:

```
az://uploads/user@example.com/reads_R1.fastq.gz
```

The results block after an upload (`ResultList.vue`) gets this right
already. The storage table is the *other* place — and the longer-lived one,
since it survives a reload and the results block does not — so it is where a
returning user goes to fetch a path. It is currently the only view that
shows a value nobody can use.

## 2. The change

- Render `f.az_path`, which `GET /files` has returned since
  [`completed/4_list_and_renew_endpoints.md`](completed/4_list_and_renew_endpoints.md).
  **Do not build it client-side** from a container name — the rule in §8 of
  [`../02-client.md`](../02-client.md) is unchanged and applies here: a
  hardcoded container is one deployment away from emitting paths that point
  at the wrong place. `blob_path` stays in the response and stays the
  `:key`; it just stops being displayed.
- Monospace the cell, and set `:title="f.az_path"` so a truncated cell is
  still readable on hover.
- Add a per-row copy-to-clipboard button, matching the one `ResultList.vue`
  already has for the whole block. Copying one path out of storage is the
  single most common thing a user will do on this page, and it is currently
  a text selection out of a truncated cell.
- Reuse `ResultList.vue`'s copy implementation rather than writing a second
  one. If that means lifting it into a small helper both import, do that —
  two clipboard code paths that can drift is the thing to avoid.
- Keep the table inside the card: `text-truncate` with the `az://` prefix
  visible is fine, but the row must not force a horizontal scrollbar on the
  page. The prefix is now ~15 characters longer than what the column was
  sized for, so re-check the `max-width` rather than leaving it at `20rem`.

## 3. Interaction with task 8

[`08-delete-files.md`](08-delete-files.md) adds a delete button to the
trailing column of the same table, and its `confirm()` prompt quotes the
same `az_path`. The two are independent — neither blocks the other — but
whichever lands second should re-read the other's section rather than
assuming the component is as its brief describes.

## 4. Tests

Vitest, on `ExistingFiles.vue`:

- The table renders `az_path` and not `blob_path` — a test that would have
  caught the current behaviour.
- The copy button puts the row's `az_path` on the clipboard, with the
  clipboard API stubbed.
- A file entry with a `null` or missing `az_path` does not render the string
  `"undefined"`. The API always sends it, but the table is fed straight from
  a network response and should degrade to `—` like the `last_modified`
  cell already does.

## 5. Out of scope

Any backend change; column sorting, filtering or pagination; a "copy all"
button for the storage table (the results block has one, and the two lists
mean different things). Renaming files, and anything about deletion — that
is task 8.
