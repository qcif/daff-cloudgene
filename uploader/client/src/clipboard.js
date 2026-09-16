import { ref } from 'vue';

// How long a "Copied!" acknowledgement stays on screen.
const COPIED_RESET_MS = 2000;

// Shared clipboard behaviour for every copy button in the app: one code path
// so the results block and the storage table cannot drift apart.
//
// `copy(text, key)` writes `text` and marks `key` as the thing just copied;
// callers with a single button can omit the key and compare `copied` against
// `true`, per-row callers pass a row identifier.
export function useCopy() {
  const copied = ref(null);
  let timer = null;

  async function copy(text, key = true) {
    await navigator.clipboard.writeText(text);
    copied.value = key;
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => { copied.value = null; }, COPIED_RESET_MS);
  }

  return { copied, copy };
}
