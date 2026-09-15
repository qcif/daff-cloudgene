import { beforeEach, describe, expect, it, vi } from 'vitest';

const stageBlock = vi.fn();
const commitBlockList = vi.fn();

vi.mock('@azure/storage-blob', () => ({
  BlockBlobClient: vi.fn().mockImplementation((url) => ({
    url,
    stageBlock,
    commitBlockList,
  })),
}));

vi.mock('../src/api.js', async () => {
  const actual = await vi.importActual('../src/api.js');
  return {
    ...actual,
    createUpload: vi.fn(),
    renewUpload: vi.fn(),
    completeUpload: vi.fn(),
  };
});

import { BlockBlobClient } from '@azure/storage-blob';
import { ApiError, createUpload, renewUpload, completeUpload } from '../src/api.js';
import { BLOCK_SIZE, STATE, UploadTask, blockCount, blockId } from '../src/upload.js';

function fakeFile(name, size) {
  return {
    name,
    size,
    slice: (start, end) => ({ start, end }),
  };
}

function futureIso(msFromNow) {
  return new Date(Date.now() + msFromNow).toISOString();
}

describe('blockId', () => {
  it('is deterministic', () => {
    expect(blockId(5)).toBe(blockId(5));
  });

  it('is equal length across a digit-count change', () => {
    const lengths = [9, 10, 999, 1000].map((i) => blockId(i).length);
    expect(new Set(lengths).size).toBe(1);
  });

  it('decodes back to a distinguishable, zero-padded id', () => {
    expect(atob(blockId(9))).toBe('block-000009');
    expect(atob(blockId(10))).toBe('block-000010');
  });
});

describe('blockCount', () => {
  it('rounds up and never returns zero', () => {
    expect(blockCount(0)).toBe(1);
    expect(blockCount(1)).toBe(1);
    expect(blockCount(BLOCK_SIZE)).toBe(1);
    expect(blockCount(BLOCK_SIZE + 1)).toBe(2);
  });
});

describe('UploadTask', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stageBlock.mockReset();
    commitBlockList.mockReset();
  });

  it('sets the content type on commitBlockList, matching the declaration', async () => {
    const file = fakeFile('reads.csv', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u1',
      upload_url: 'https://blob/u1',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u1',
    });
    stageBlock.mockResolvedValue(undefined);
    commitBlockList.mockResolvedValue(undefined);
    completeUpload.mockResolvedValue({ state: 'completed', az_path: 'az://uploads/u1' });

    const task = new UploadTask(file);
    await task.start();

    expect(commitBlockList).toHaveBeenCalledWith(
      [blockId(0)],
      expect.objectContaining({
        blobHTTPHeaders: { blobContentType: 'text/csv' },
      }),
    );
    expect(task.state).toBe(STATE.COMPLETE);
  });

  it('resume re-stages only the missing indices', async () => {
    const size = 3 * BLOCK_SIZE;
    const file = fakeFile('big.gz', size);
    createUpload.mockResolvedValue({
      upload_id: 'u2',
      upload_url: 'https://blob/u2',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u2',
    });

    // Blocks 0 and 1 stage fine; block 2 fails once (simulated exhausted
    // retry), then succeeds on resume.
    let attempt = 0;
    stageBlock.mockImplementation(async (id) => {
      attempt += 1;
      if (id === blockId(2) && attempt <= 3) {
        throw new Error('network down');
      }
    });
    commitBlockList.mockResolvedValue(undefined);
    completeUpload.mockResolvedValue({ state: 'completed', az_path: 'az://uploads/u2' });

    const task = new UploadTask(file);
    await task.start();
    expect(task.state).toBe(STATE.FAILED);

    const stagedBeforeResume = stageBlock.mock.calls.map(([id]) => id);
    expect(stagedBeforeResume).toContain(blockId(0));
    expect(stagedBeforeResume).toContain(blockId(1));

    stageBlock.mockClear();
    stageBlock.mockImplementation(async () => undefined);

    await task.resume();

    const stagedOnResume = stageBlock.mock.calls.map(([id]) => id);
    expect(stagedOnResume).toEqual([blockId(2)]);
    expect(task.state).toBe(STATE.COMPLETE);
  });

  it('renews proactively when the SAS is close to expiry, not after', async () => {
    const file = fakeFile('near-expiry.gz', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u3',
      upload_url: 'https://blob/u3',
      expires_at: futureIso(30 * 60 * 1000), // 30 min: under the 1h threshold
      az_path: 'az://uploads/u3',
    });
    renewUpload.mockResolvedValue({
      upload_id: 'u3',
      upload_url: 'https://blob/u3-renewed',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u3',
    });
    stageBlock.mockResolvedValue(undefined);
    commitBlockList.mockResolvedValue(undefined);
    completeUpload.mockResolvedValue({ state: 'completed', az_path: 'az://uploads/u3' });

    const task = new UploadTask(file);
    await task.start();

    expect(renewUpload).toHaveBeenCalledWith('u3');
    expect(task.state).toBe(STATE.COMPLETE);
  });

  it('does not renew when the SAS is fresh', async () => {
    const file = fakeFile('fresh.gz', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u4',
      upload_url: 'https://blob/u4',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u4',
    });
    stageBlock.mockResolvedValue(undefined);
    commitBlockList.mockResolvedValue(undefined);
    completeUpload.mockResolvedValue({ state: 'completed', az_path: 'az://uploads/u4' });

    const task = new UploadTask(file);
    await task.start();

    expect(renewUpload).not.toHaveBeenCalled();
  });

  it('surfaces a renewal 409 as "start again", not a retry loop', async () => {
    const file = fakeFile('doomed.gz', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u5',
      upload_url: 'https://blob/u5',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u5',
    });
    // Azure rejects with 403 mid-upload; the reactive renewal is then
    // refused because the record is already terminal server-side.
    stageBlock.mockRejectedValue(Object.assign(new Error('forbidden'), { statusCode: 403 }));
    renewUpload.mockRejectedValue(new ApiError(409, 'terminal'));

    const task = new UploadTask(file);
    await task.start();

    expect(task.state).toBe(STATE.FAILED);
    expect(task.error.toLowerCase()).toContain('start again');
    // No infinite retry loop: renewUpload is called exactly once per stage
    // attempt, not repeatedly.
    expect(renewUpload).toHaveBeenCalledTimes(1);
  });

  it('marks the task failed (not complete) when reconciliation fails', async () => {
    const file = fakeFile('mismatch.gz', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u6',
      upload_url: 'https://blob/u6',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u6',
    });
    stageBlock.mockResolvedValue(undefined);
    commitBlockList.mockResolvedValue(undefined);
    completeUpload.mockResolvedValue({
      state: 'failed',
      detail: "Blob content type 'application/octet-stream' does not match the declared 'text/csv'",
    });

    const task = new UploadTask(fakeFile('mismatch.csv', 10));
    await task.start();

    expect(task.state).toBe(STATE.FAILED);
    expect(task.error).toContain('does not match the declared');
  });

  it('cancel() aborts before commit and moves to CANCELLED', async () => {
    const file = fakeFile('cancel-me.gz', 10);
    createUpload.mockResolvedValue({
      upload_id: 'u7',
      upload_url: 'https://blob/u7',
      expires_at: futureIso(24 * 60 * 60 * 1000),
      az_path: 'az://uploads/u7',
    });

    const task = new UploadTask(file);
    stageBlock.mockImplementation(async () => {
      task.cancel();
    });

    await task.start();

    expect(task.state).toBe(STATE.CANCELLED);
    expect(commitBlockList).not.toHaveBeenCalled();
  });
});
