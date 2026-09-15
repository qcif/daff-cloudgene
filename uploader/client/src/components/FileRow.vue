<script setup>
import { computed } from 'vue';
import { STATE } from '../upload.js';

const props = defineProps({
  task: { type: Object, required: true },
});
const emit = defineEmits(['cancel', 'retry']);

const percent = computed(() => Math.round(props.task.progress * 100));

const badgeClass = computed(() => ({
  [STATE.QUEUED]: 'badge-secondary',
  [STATE.REQUESTING]: 'badge-info',
  [STATE.UPLOADING]: 'badge-info',
  [STATE.COMPLETING]: 'badge-info',
  [STATE.COMPLETE]: 'badge-success',
  [STATE.FAILED]: 'badge-danger',
  [STATE.CANCELLED]: 'badge-secondary',
}[props.task.state] || 'badge-secondary'));

const canCancel = computed(() => !props.task.isTerminal);
const canRetry = computed(() => props.task.state === STATE.FAILED);
</script>

<template>
  <div class="d-flex align-items-center py-2 border-bottom">
    <div class="flex-grow-1 mr-3" style="min-width: 0">
      <div class="text-truncate">{{ task.file.name }}</div>
      <div class="progress" style="height: 6px" v-if="!task.isTerminal">
        <div
          class="progress-bar"
          role="progressbar"
          :style="{ width: percent + '%' }"
          :aria-valuenow="percent"
          aria-valuemin="0"
          aria-valuemax="100"
        ></div>
      </div>
      <small v-if="task.state === STATE.FAILED" class="text-danger">
        {{ task.error }}
      </small>
    </div>
    <span class="badge mr-2" :class="badgeClass">{{ task.state }}</span>
    <button
      v-if="canCancel"
      type="button"
      class="btn btn-sm btn-outline-secondary"
      @click="emit('cancel')"
    >
      Cancel
    </button>
    <button
      v-if="canRetry"
      type="button"
      class="btn btn-sm btn-outline-primary"
      @click="emit('retry')"
    >
      Retry
    </button>
  </div>
</template>
