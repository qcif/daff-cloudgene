<script setup>
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
          <td class="text-truncate" style="max-width: 20rem">
            {{ f.blob_path }}
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
