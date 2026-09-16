import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import ExistingFiles from '../src/components/ExistingFiles.vue';

const FILES = [
  {
    blob_path: 'user@example.com/reads_R1.fastq.gz',
    az_path: 'az://uploads/user@example.com/reads_R1.fastq.gz',
    client_path: 'reads_R1.fastq.gz',
    size: 12000000,
    last_modified: '2026-09-17T01:02:03Z',
    state: 'completed',
  },
  {
    blob_path: 'user@example.com/reads_R2.fastq.gz',
    az_path: 'az://uploads/user@example.com/reads_R2.fastq.gz',
    client_path: 'reads_R2.fastq.gz',
    size: 13000000,
    last_modified: null,
    state: 'completed',
  },
];

let writeText;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
});

describe('ExistingFiles', () => {
  it('renders the az:// path, not the blob path', () => {
    const wrapper = mount(ExistingFiles, { props: { files: FILES } });
    const cells = wrapper.findAll('tbody tr td span.text-truncate');

    // Cell text is exactly the az_path: the bare blob_path is nowhere in it.
    expect(cells.map((c) => c.text())).toEqual(FILES.map((f) => f.az_path));
  });

  it('exposes the full path as a title so truncation stays readable', () => {
    const wrapper = mount(ExistingFiles, { props: { files: FILES } });
    const cell = wrapper.find('tbody tr td span.text-truncate');

    expect(cell.attributes('title')).toBe(FILES[0].az_path);
    expect(cell.classes()).toContain('text-truncate');
    expect(cell.classes()).toContain('text-monospace');
  });

  it('copies that row az_path to the clipboard', async () => {
    const wrapper = mount(ExistingFiles, { props: { files: FILES } });
    const buttons = wrapper.findAll('tbody tr button.btn-link');

    expect(buttons).toHaveLength(FILES.length);
    await buttons[1].trigger('click');

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(FILES[1].az_path);
  });

  it('degrades to an em dash when az_path is missing', () => {
    const files = [
      { ...FILES[0], az_path: null },
      { ...FILES[1], az_path: undefined },
    ];
    const wrapper = mount(ExistingFiles, { props: { files } });

    expect(wrapper.text()).not.toContain('undefined');
    expect(wrapper.text()).not.toContain('null');
    expect(wrapper.findAll('tbody tr td')[0].text()).toBe('—');
    // Nothing to copy, so no copy button on those rows.
    expect(wrapper.findAll('tbody tr button.btn-link')).toHaveLength(0);
  });

  it('asks for confirmation naming the az path before emitting a delete', async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    const wrapper = mount(ExistingFiles, { props: { files: FILES } });

    await wrapper.findAll('tbody tr button.btn-outline-danger')[1].trigger('click');

    const [prompt] = confirm.mock.calls[0];
    expect(prompt).toContain(FILES[1].az_path);
    expect(prompt).toContain('cannot be undone');
    expect(wrapper.emitted('delete')[0]).toEqual([FILES[1]]);
  });

  it('emits nothing when the confirmation is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    const wrapper = mount(ExistingFiles, { props: { files: FILES } });

    await wrapper.find('tbody tr button.btn-outline-danger').trigger('click');

    expect(wrapper.emitted('delete')).toBeUndefined();
  });

  it('disables only the row whose delete is in flight', () => {
    const wrapper = mount(ExistingFiles, {
      props: { files: FILES, deleting: [FILES[0].blob_path] },
    });
    const buttons = wrapper.findAll('tbody tr button.btn-outline-danger');

    expect(buttons[0].attributes('disabled')).toBeDefined();
    expect(buttons[1].attributes('disabled')).toBeUndefined();
  });

  it('shows a delete error against its own row, leaving the row in place', () => {
    const wrapper = mount(ExistingFiles, {
      props: {
        files: FILES,
        deleteErrors: { [FILES[0].blob_path]: 'An upload is in progress' },
      },
    });

    expect(wrapper.text()).toContain('An upload is in progress');
    expect(wrapper.findAll('tbody tr td span.text-truncate')).toHaveLength(
      FILES.length);
  });

  it('shows the empty and loading states without a table', () => {
    expect(mount(ExistingFiles, { props: { files: [], loading: true } }).text())
      .toContain('Loading your files');
    expect(mount(ExistingFiles, { props: { files: [] } }).text())
      .toContain('no files in storage');
  });
});
