// One file's upload state machine: queued -> requesting -> uploading ->
// completing -> complete, with failed/cancelled terminal. See
// uploader/spec/tasks/5_build_client.md §7.
//
// Block staging is hand-rolled (not uploadData()) because resumability
// needs deterministic block IDs across retries — uploadData() generates
// fresh ones on every call, so a retry would re-upload the whole file
// (§7.1). This is the one place the design spec's "do not hand-roll the
// block staging API" guidance is deliberately overridden.

import { BlockBlobClient } from '@azure/storage-blob';
import { createUpload, renewUpload, completeUpload, ApiError } from './api.js';
import { contentTypeFor } from './validate.js';

export const BLOCK_SIZE = 4 * 1024 * 1024;
export const CONCURRENCY = 6;
export const RENEW_BEFORE_EXPIRY_MS = 60 * 60 * 1000;

export const STATE = Object.freeze({
  QUEUED: 'queued',
  REQUESTING: 'requesting',
  UPLOADING: 'uploading',
  COMPLETING: 'completing',
  COMPLETE: 'complete',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
});

const TERMINAL_STATES = new Set([
  STATE.COMPLETE, STATE.FAILED, STATE.CANCELLED,
]);

// Deterministic, equal-length block IDs. Azure requires every block ID in a
// blob to be the same length, so the zero-padding must be wide enough for
// the largest file this deployment will ever see: 500 GiB
// (UPLOADER_MAX_UPLOAD_BYTES default) / 4 MiB blocks is ~131,000 blocks, well
// under 6 digits' 999,999 ceiling.
export function blockId(index) {
  return btoa(`block-${String(index).padStart(6, '0')}`);
}

export function blockCount(size) {
  return Math.max(1, Math.ceil(size / BLOCK_SIZE));
}

class TerminalUploadError extends Error {}

// Runs `worker(item)` over `items` with bounded concurrency, stopping (and
// rejecting) on the first failure that isn't a transparent retry.
async function runPool(items, concurrency, worker) {
  let cursor = 0;
  let firstError = null;

  async function runNext() {
    while (true) {
      if (firstError) return;
      const index = cursor;
      if (index >= items.length) return;
      cursor += 1;
      try {
        await worker(items[index]);
      } catch (err) {
        firstError = firstError || err;
        return;
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, runNext));

  if (firstError) throw firstError;
}

// One file's upload lifecycle. Constructed per selected file; `onChange` is
// called with `(task)` on every state or progress update so a component can
// re-render.
export class UploadTask {
  constructor(file, { onChange = () => {} } = {}) {
    this.file = file;
    this.contentType = contentTypeFor(file.name);
    this.onChange = onChange;

    this.state = STATE.QUEUED;
    this.error = null;
    this.bytesStaged = 0;
    this.record = null;

    this._stagedIndices = new Set();
    this._blockBlobClient = null;
    this._abortController = null;
  }

  get progress() {
    if (!this.file.size) return 0;
    return Math.min(1, this.bytesStaged / this.file.size);
  }

  get isTerminal() {
    return TERMINAL_STATES.has(this.state);
  }

  _setState(state, patch = {}) {
    this.state = state;
    Object.assign(this, patch);
    this.onChange(this);
  }

  cancel() {
    if (this.isTerminal) return;
    if (this._abortController) this._abortController.abort();
    this._setState(STATE.CANCELLED);
  }

  // Entry point for a fresh upload, and for a retry after failure — both
  // request a token and (re)stage whatever is missing. `record` from a
  // prior POST /uploads is not reusable across attempts once it has failed
  // or expired past renewal, so a hard retry starts a new record.
  async start() {
    this._abortController = new AbortController();

    try {
      this._setState(STATE.REQUESTING);
      this.record = await createUpload({
        filename: this.file.name,
        size: this.file.size,
        contentType: this.contentType,
      });
      this._blockBlobClient = new BlockBlobClient(this.record.upload_url);

      await this._uploadAndComplete();
    } catch (err) {
      this._fail(err);
    }
  }

  // Resumes an in-session upload after a transient failure: stages only the
  // blocks not yet in `_stagedIndices`. Does not request a new record.
  async resume() {
    if (!this.record) return this.start();

    this._abortController = new AbortController();
    try {
      await this._uploadAndComplete();
    } catch (err) {
      this._fail(err);
    }
  }

  async _uploadAndComplete() {
    this._setState(STATE.UPLOADING);

    const total = blockCount(this.file.size);
    const indices = Array.from({ length: total }, (_, i) => i)
      .filter((i) => !this._stagedIndices.has(i));

    await runPool(indices, CONCURRENCY, (index) => this._stageOne(index));

    if (this._abortController.signal.aborted) {
      throw new TerminalUploadError('cancelled');
    }

    const allIds = Array.from({ length: total }, (_, i) => blockId(i));
    await this._blockBlobClient.commitBlockList(allIds, {
      blobHTTPHeaders: { blobContentType: this.contentType },
      abortSignal: this._abortController.signal,
    });

    this._setState(STATE.COMPLETING);
    const result = await completeUpload(this.record.upload_id);
    this.record = result;

    if (result.state === 'completed') {
      this._setState(STATE.COMPLETE, { record: result });
    } else {
      // 'failed' from reconciliation — a real mismatch, not a transport
      // error. detail names the failing check verbatim.
      this._setState(STATE.FAILED, {
        record: result,
        error: result.detail || 'Upload failed reconciliation',
      });
    }
  }

  async _stageOne(index) {
    await this._maybeRenewProactively();

    const start = index * BLOCK_SIZE;
    const end = Math.min(start + BLOCK_SIZE, this.file.size);
    const chunk = this.file.slice(start, end);
    const size = end - start;

    try {
      await this._stageChunk(index, chunk, size);
    } catch (err) {
      if (this._isForbidden(err)) {
        await this._renewReactively();
        await this._stageChunk(index, chunk, size);
        return;
      }
      throw err;
    }

    this._stagedIndices.add(index);
    this.bytesStaged += size;
    this.onChange(this);
  }

  async _stageChunk(index, chunk, size) {
    await this._blockBlobClient.stageBlock(blockId(index), chunk, size, {
      abortSignal: this._abortController.signal,
    });
  }

  _isForbidden(err) {
    return err && (err.statusCode === 403 || err.code === 'AuthorizationFailure');
  }

  async _maybeRenewProactively() {
    if (!this.record?.expires_at) return;
    const remaining = new Date(this.record.expires_at).getTime() - Date.now();
    if (remaining > RENEW_BEFORE_EXPIRY_MS) return;
    await this._renewReactively();
  }

  async _renewReactively() {
    let renewed;
    try {
      renewed = await renewUpload(this.record.upload_id);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 409)) {
        throw new TerminalUploadError(
          'This upload can no longer be renewed. Start again.');
      }
      throw err;
    }
    this.record = renewed;
    this._blockBlobClient = new BlockBlobClient(renewed.upload_url);
  }

  _fail(err) {
    if (this.state === STATE.CANCELLED) return;
    const message = err instanceof TerminalUploadError
      ? err.message
      : (err && err.detail) || (err && err.message) || 'Upload failed';
    this._setState(STATE.FAILED, { error: message });
  }
}
