<script setup>
import { useCopy } from '../clipboard.js';

defineProps({
  files: { type: Array, required: true },
  truncated: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
});

const badgeClass = {
  pending: 'badge-info',
  completed: 'badge-success',
  failed: 'badge-danger',
  expired: 'badge-secondary',
};

// Keyed by blob_path, so only the row just copied shows the acknowledgement.
const { copied, copy } = useCopy();
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
        </tr>
      </thead>
      <tbody>
        <tr v-for="f in files" :key="f.blob_path">
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
        </tr>
      </tbody>
    </table>
    <p v-if="truncated" class="text-muted small">
      Showing only the first files in storage; more exist but are not listed.
    </p>
  </div>
</template>
