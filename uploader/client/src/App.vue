<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import FileRow from './components/FileRow.vue';
import ExistingFiles from './components/ExistingFiles.vue';
import ResultList from './components/ResultList.vue';
import { UploadTask, STATE } from './upload.js';
import { validateFile, ValidationError, ACCEPT_ATTRIBUTE } from './validate.js';
import { listFiles, deleteFile, ApiError, AUTH_REDIRECT } from './api.js';

const tasks = reactive([]);
const rejections = ref([]);
const existingFiles = ref([]);
const existingTruncated = ref(false);
const existingLoading = ref(true);
const listError = ref(null);
// Set when there is no usable Cloudgene session and no redirect happened,
// so the page must explain itself rather than sit there empty.
const authError = ref(null);

const completedPaths = ref([]);

// Blob paths with a delete in flight, and blob path -> message for the ones
// that failed. Both are per row: one refused delete must not clear the
// table or the upload results.
const deletingPaths = ref([]);
const deleteErrors = ref({});

async function onDelete(file) {
  const key = file.blob_path;
  deletingPaths.value = [...deletingPaths.value, key];
  deleteErrors.value = { ...deleteErrors.value, [key]: undefined };

  try {
    await deleteFile(file.client_path);
    // `deleted: false` — the file was already gone and the list was stale —
    // takes this same path. Drop the row so the UI answers immediately,
    // then reload so the table reflects Azure rather than a guess.
    existingFiles.value = existingFiles.value.filter(
      (f) => f.blob_path !== key);
    await loadExistingFiles();
  } catch (err) {
    // AUTH_REDIRECT means the page is already navigating away; anything
    // else belongs against this row, verbatim — a 409 is actionable ("an
    // upload to this path is in progress"), so it must not be reworded.
    if (err instanceof ApiError && err.status !== AUTH_REDIRECT) {
      deleteErrors.value = { ...deleteErrors.value, [key]: err.detail };
    }
  } finally {
    deletingPaths.value = deletingPaths.value.filter((p) => p !== key);
  }
}

async function loadExistingFiles() {
  existingLoading.value = true;
  listError.value = null;
  try {
    const result = await listFiles();
    existingFiles.value = result.files;
    existingTruncated.value = result.truncated;
  } catch (err) {
    // AUTH_REDIRECT means the browser is already navigating to the login;
    // there is nothing to show. Everything else, including a 401 that did
    // not redirect, is worth putting in front of the user.
    if (err instanceof ApiError && err.status !== AUTH_REDIRECT) {
      if (err.status === 401 || err.status === 403) {
        // Shown once, at the top of the page — not repeated as a
        // file-listing error, which would say the same thing twice.
        authError.value = err.detail;
      } else {
        listError.value = err.detail;
      }
    }
  } finally {
    existingLoading.value = false;
  }
}

function onFilesPicked(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = '';
  rejections.value = [];

  for (const file of files) {
    try {
      validateFile(file);
    } catch (err) {
      if (err instanceof ValidationError) {
        rejections.value.push(`${file.name}: ${err.message}`);
        continue;
      }
      throw err;
    }

    // Wrapped in reactive() before any method is called on it, so that
    // methods invoked as `task.start()` run with `this` bound to the proxy
    // — plain field assignment inside the class (`this.state = ...`) then
    // goes through Vue's reactivity, and the template updates without any
    // manual re-render trigger.
    const task = reactive(new UploadTask(file, { onChange: handleTaskChange }));
    tasks.push(task);
    task.start();
  }
}

function handleTaskChange(task) {
  if (task.state === STATE.COMPLETE && task.record?.az_path) {
    if (!completedPaths.value.includes(task.record.az_path)) {
      completedPaths.value = [...completedPaths.value, task.record.az_path];
    }
    loadExistingFiles();
  }
}

function cancelTask(task) {
  task.cancel();
}

function retryTask(task) {
  task.resume();
}

onMounted(loadExistingFiles);

onBeforeUnmount(() => {
  for (const task of tasks) {
    if (!task.isTerminal) task.cancel();
  }
});
</script>

<template>
  <nav class="navbar navbar-expand-md fixed-top navbar-dark" style="background: rgb(52, 58, 64)">
    <div class="container d-flex justify-content-between">
      <a class="navbar-brand" href="/#!pages/home">DAFF Biosecurity workflows</a>
      <button
        class="navbar-toggler"
        type="button"
        data-toggle="collapse"
        data-target="#navbarsExampleDefault"
        aria-controls="navbarsExampleDefault"
        aria-expanded="false"
        aria-label="Toggle navigation"
      >
        <span class="navbar-toggler-icon"></span>
      </button>

      <div class="collapse navbar-collapse" id="navbarsExampleDefault">
        <ul class="navbar-nav mr-auto">
          <li class="nav-item">
            <a class="nav-link" href="/#!pages/home">Home</a>
          </li>

          <li class="nav-item dropdown">
            <a
              class="nav-link dropdown-toggle"
              href="#"
              id="dropdown01"
              data-toggle="dropdown"
              aria-haspopup="true"
              aria-expanded="false"
              >Run</a
            >
            <div class="dropdown-menu" aria-labelledby="dropdown01">
              <a class="dropdown-item" href="/#!run/taxodactyl@1.2.0"
                >Taxodactyl <small class="text-muted">1.2.0</small></a
              >
            </div>
          </li>

          <li class="nav-item">
            <a class="nav-link" href="/#!pages/jobs">Jobs</a>
          </li>

          <li class="nav-item active">
            <a class="nav-link" href="/uploads/">Upload <span class="sr-only">(current)</span></a>
          </li>

          <li class="nav-item">
            <a class="nav-link" href="/download">Download</a>
          </li>

          <li class="nav-item dropdown">
            <a class="nav-link dropdown-toggle" href="#" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false" id="docs">Docs</a>
            <div class="dropdown-menu" aria-labelledby="docs">
                <a class="dropdown-item" href="/#!pages/taxodactyl">Taxodactyl</a>
            </div>
          </li>

          <li class="nav-item">
            <a class="nav-link" href="/#!pages/contact">Contact</a>
          </li>
        </ul>
        <ul class="navbar-nav my-2 my-lg-0">
          <li class="nav-item dropdown">
            <a
              class="nav-link dropdown-toggle"
              href="#"
              id="dropdown02"
              data-toggle="dropdown"
              aria-haspopup="true"
              aria-expanded="false"
              ><i class="fas fa-user"></i> User</a
            >
            <div class="dropdown-menu" aria-labelledby="dropdown02">
              <a class="dropdown-item" href="/#!pages/profile">Profile</a>
              <div class="dropdown-divider"></div>

              <a class="dropdown-item" href="/#!pages/logout">Logout</a>
            </div>
          </li>
        </ul>
      </div>
    </div>
  </nav>

  <div class="container mt-5 pt-5">
    <div class="row justify-content-center">
      <div class="col">
        <div class="card shadow mb-4">
          <div class="card-body">
            <h4 class="card-title mb-4">Upload files</h4>

            <div v-if="authError" class="alert alert-warning">
              <strong>Not signed in.</strong>
              <div>{{ authError }}</div>
            </div>

            <div class="form-group">
              <label for="fileInput" class="font-weight-bold">
                Choose files to upload
              </label>
              <input
                id="fileInput"
                type="file"
                class="form-control-file"
                multiple
                :accept="ACCEPT_ATTRIBUTE"
                @change="onFilesPicked"
              />
            </div>

            <div v-if="rejections.length" class="alert alert-danger">
              <div v-for="(msg, i) in rejections" :key="i">{{ msg }}</div>
            </div>

            <div v-if="tasks.length" class="mt-3">
              <FileRow
                v-for="(task, i) in tasks"
                :key="i"
                :task="task"
                @cancel="cancelTask(task)"
                @retry="retryTask(task)"
              />
            </div>
          </div>
        </div>

        <ResultList :paths="completedPaths" class="mb-4" />

        <div class="card shadow">
          <div class="card-body">
            <h5 class="card-title mb-3">Your files in storage</h5>
            <div v-if="listError" class="alert alert-danger">{{ listError }}</div>
            <ExistingFiles
              :files="existingFiles"
              :truncated="existingTruncated"
              :loading="existingLoading"
              :deleting="deletingPaths"
              :delete-errors="deleteErrors"
              @delete="onDelete"
            />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
