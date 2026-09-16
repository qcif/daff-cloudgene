import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import ResultList from '../src/components/ResultList.vue';

const PATHS = [
  'az://uploads/user@example.com/reads_R1.fastq.gz',
  'az://uploads/user@example.com/reads_R2.fastq.gz',
];

let writeText;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
});

// ResultList and ExistingFiles share src/clipboard.js; this covers the
// whole-block button so the shared helper cannot regress one of them.
describe('ResultList', () => {
  it('copies every path as one newline-delimited block', async () => {
    const wrapper = mount(ResultList, { props: { paths: PATHS } });

    await wrapper.find('button').trigger('click');

    expect(writeText).toHaveBeenCalledWith(PATHS.join('\n'));
  });

  it('acknowledges the copy on the button', async () => {
    const wrapper = mount(ResultList, { props: { paths: PATHS } });
    const button = wrapper.find('button');

    expect(button.text()).toContain('Copy all paths');
    await button.trigger('click');
    await wrapper.vm.$nextTick();

    expect(button.text()).toContain('Copied!');
  });

  it('renders nothing without paths', () => {
    expect(mount(ResultList, { props: { paths: [] } }).find('div').exists())
      .toBe(false);
  });
});
