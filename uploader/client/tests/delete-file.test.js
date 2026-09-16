// The delete flow end to end through App.vue: the component confirms and
// emits, App.vue is the only thing that calls api.js, and the outcome lands
// back on the row it came from (08-delete-files.md §4).

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

vi.mock('../src/api.js', async (importOriginal) => {
  // ApiError and AUTH_REDIRECT come from the real module: App.vue branches
  // on `err instanceof ApiError`, so a hand-written stub error class would
  // make the test pass down a path production never takes.
  const actual = await importOriginal();
  return {
    ...actual,
    listFiles: vi.fn(),
    deleteFile: vi.fn(),
  };
});

import App from '../src/App.vue';
import { ApiError, deleteFile, listFiles } from '../src/api.js';

const FILE = {
  blob_path: 'user@example.com/reads_R1.fastq.gz',
  az_path: 'az://uploads/user@example.com/reads_R1.fastq.gz',
  client_path: 'reads_R1.fastq.gz',
  size: 12000000,
  last_modified: '2026-09-17T01:02:03Z',
  state: 'completed',
};

function listing(...files) {
  return { files, truncated: false };
}

async function mountApp() {
  const wrapper = mount(App);
  await flushPromises();
  return wrapper;
}

function deleteButton(wrapper) {
  return wrapper.find('tbody tr button.btn-outline-danger');
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn() } });
  listFiles.mockResolvedValue(listing(FILE));
});

describe('deleting a file', () => {
  it('sends the row client_path, not the blob path', async () => {
    deleteFile.mockResolvedValue({ deleted: true, blob_path: FILE.blob_path });
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(deleteFile).toHaveBeenCalledWith(FILE.client_path);
  });

  it('drops the row and reloads the list from the API', async () => {
    deleteFile.mockResolvedValue({ deleted: true, blob_path: FILE.blob_path });
    listFiles.mockResolvedValueOnce(listing(FILE)).mockResolvedValue(listing());
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(listFiles).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain('no files in storage');
  });

  it('treats deleted: false the same as a real delete', async () => {
    // The file was already gone and the list was stale; the UI answer is
    // identical, which is why the route is not a 404.
    deleteFile.mockResolvedValue({ deleted: false, blob_path: FILE.blob_path });
    listFiles.mockResolvedValueOnce(listing(FILE)).mockResolvedValue(listing());
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('no files in storage');
  });

  it('leaves the row in place and shows the message on a 409', async () => {
    const detail = 'An upload to this path is in progress; cancel or wait.';
    deleteFile.mockRejectedValue(new ApiError(409, detail));
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain(detail);
    expect(wrapper.text()).toContain(FILE.az_path);
    // Not reloaded, and the row is still deletable once the upload ends.
    expect(listFiles).toHaveBeenCalledTimes(1);
    expect(deleteButton(wrapper).attributes('disabled')).toBeUndefined();
  });

  it('keeps a failed delete out of the page-level error area', async () => {
    deleteFile.mockRejectedValue(new ApiError(503, 'Try again shortly.'));
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(wrapper.find('.alert-danger').exists()).toBe(false);
    expect(wrapper.text()).toContain('Try again shortly.');
  });

  it('calls the API not at all when the confirmation is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    const wrapper = await mountApp();

    await deleteButton(wrapper).trigger('click');
    await flushPromises();

    expect(deleteFile).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain(FILE.az_path);
  });
});
