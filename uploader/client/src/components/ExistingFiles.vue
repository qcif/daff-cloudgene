<script setup>
import { useCopy } from '../clipboard.js';

// The table is read-only apart from one destructive action per row. That
// action is confirmed here but performed by the parent: the component emits
// `delete` rather than calling api.js, so App.vue stays the only module
// that talks to the API. In-flight and per-row error state come back down
// as props for the same reason.
defineProps({
  files: { type: Array, required: true },
  truncated: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  // Blob paths with a delete in flight, so one row's button is disabled
  // rather than the whole table.
  deleting: { type: Array, default: () => [] },
  // Blob path -> message. A failed delete is a row-level problem; it must
  // not clear the table or the upload results above it.
  deleteErrors: { type: Object, default: () => ({}) },
});

const emit = defineEmits(['delete']);

const badgeClass = {
  pending: 'badge-info',
  completed: 'badge-success',
  failed: 'badge-danger',
  expired: 'badge-secondary',
};

// Keyed by blob_path, so only the row just copied shows the acknowledgement.
const { copied, copy } = useCopy();

// A native confirm() rather than a modal component: this page is plain
// Bootstrap and this is one dialog. The prompt names the full az:// path
// and says the delete cannot be undone — nothing on the storage account
// enables blob soft delete, so assume there is no undelete.
function confirmDelete(file) {
  const path = file.az_path || file.blob_path;
  if (!window.confirm(`Delete ${path}?\nThis cannot be undone.`)) {
    return;
  }
  emit('delete', file);
}
</script>

<template>
  <div>
    <p v-if="loading" class="text-muted">Loading your files…</p>
    <p v-else-if="!files.length" class="text-muted">
      You have no files in storage yet.
    </p>
    <table v-else class="table table-sm">
      <thead>
        <tr>
          <th>Path</th>
          <th>Size</th>
          <th>Last modified</th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <template v-for="f in files" :key="f.blob_path">
        <tr>
          <td style="max-width: 28rem">
            <div class="d-flex align-items-center">
              <span class="text-truncate text-monospace" :title="f.az_path">
                {{ f.az_path || '—' }}
              </span>
              <button
                v-if="f.az_path"
                type="button"
                class="btn btn-sm btn-link p-0 ml-2 flex-shrink-0"
                :title="`Copy ${f.az_path}`"
                @click="copy(f.az_path, f.blob_path)"
              >
                <i class="fas" :class="copied === f.blob_path ? 'fa-check' : 'fa-copy'"></i>
                <span class="sr-only">Copy path</span>
              </button>
            </div>
          </td>
          <td>{{ (f.size / 1e6).toFixed(1) }} MB</td>
          <td>{{ f.last_modified ? new Date(f.last_modified).toLocaleString() : '—' }}</td>
          <td>
            <span v-if="f.state" class="badge" :class="badgeClass[f.state] || 'badge-secondary'">
              {{ f.state }}
            </span>
          </td>
          <td class="text-right">
            <button
              type="button"
              class="btn btn-sm btn-outline-danger"
              :disabled="deleting.includes(f.blob_path)"
              :title="`Delete ${f.az_path || f.blob_path}`"
              @click="confirmDelete(f)"
            >
              <i class="fas fa-trash"></i>
              <span class="sr-only">Delete</span>
            </button>
          </td>
        </tr>
        <tr v-if="deleteErrors[f.blob_path]">
          <td colspan="5" class="border-0 pt-0 text-danger small">
            {{ deleteErrors[f.blob_path] }}
          </td>
        </tr>
        </template>
      </tbody>
    </table>
    <p v-if="truncated" class="text-muted small">
      Showing only the first files in storage; more exist but are not listed.
    </p>
  </div>
</template>
