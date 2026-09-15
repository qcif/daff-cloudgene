<script setup>
import { computed, ref } from 'vue';

const props = defineProps({
  paths: { type: Array, required: true },
});

const text = computed(() => props.paths.join('\n'));
const copied = ref(false);

async function copy() {
  await navigator.clipboard.writeText(text.value);
  copied.value = true;
  setTimeout(() => { copied.value = false; }, 2000);
}
</script>

<template>
  <div v-if="paths.length" class="card">
    <div class="card-body">
      <h5 class="card-title">Uploaded files</h5>
      <pre class="bg-light p-2 border rounded"><code>{{ text }}</code></pre>
      <button type="button" class="btn btn-primary" @click="copy">
        <i class="fas fa-copy mr-2"></i>{{ copied ? 'Copied!' : 'Copy all paths' }}
      </button>
    </div>
  </div>
</template>
