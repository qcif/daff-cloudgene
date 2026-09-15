// fetch wrapper for the uploader's own API (uploader/spec/01-api.md §3).
// Attaches X-Auth-Token on every call, reading it fresh each time (auth.js),
// and maps every status code to the behaviour that section specifies.

import { getToken, redirectToLogin, notSignedInMessage } from './auth.js';

export const API_BASE = '/uploads/api';

// Thrown for every non-2xx response, and for the redirect case. `status` is
// null only for AUTH_REDIRECT, where the browser is already navigating away
// and there is nothing left for a caller to do but stop.
export const AUTH_REDIRECT = 'AUTH_REDIRECT';

export class ApiError extends Error {
  constructor(status, detail, { retryAfterSeconds = null } = {}) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

const GENERIC_UNAVAILABLE = 'Try again shortly.';

// There is no usable session. If the browser is navigating to the login,
// the error is a no-op the UI should swallow — the page is going away. If
// it is not (dev, or an already-spent redirect guard), the user needs to be
// told what to do, so this becomes an ordinary displayable 401.
function noSessionError() {
  return redirectToLogin()
    ? new ApiError(AUTH_REDIRECT, 'Not signed in to Cloudgene')
    : new ApiError(401, notSignedInMessage());
}

async function request(path, options = {}) {
  const token = getToken();
  if (!token) {
    throw noSessionError();
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        ...(options.headers || {}),
        'X-Auth-Token': token,
      },
    });
  } catch (err) {
    // A network-level failure (offline, DNS, CORS) — treat the same as a
    // 503: not a login problem, worth retrying shortly.
    throw new ApiError(503, GENERIC_UNAVAILABLE);
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // No body, or not JSON — fine for a 2xx with no content.
  }

  if (response.ok) {
    return body;
  }

  const detail = (body && body.detail) || response.statusText;

  if (response.status === 401) {
    throw noSessionError();
  }

  if (response.status === 503) {
    throw new ApiError(503, GENERIC_UNAVAILABLE);
  }

  if (response.status === 429) {
    const retryAfter = response.headers.get('Retry-After');
    throw new ApiError(429, detail, {
      retryAfterSeconds: retryAfter ? Number(retryAfter) : null,
    });
  }

  // 400, 403, 404, 409: detail is shown verbatim per the spec's table.
  throw new ApiError(response.status, detail);
}

export function createUpload({ filename, size, contentType }) {
  return request('/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename,
      size,
      content_type: contentType,
    }),
  });
}

export function renewUpload(uploadId) {
  return request(`/uploads/${encodeURIComponent(uploadId)}/renew`, {
    method: 'POST',
  });
}

export function completeUpload(uploadId) {
  return request(`/uploads/${encodeURIComponent(uploadId)}/complete`, {
    method: 'POST',
  });
}

export function getUpload(uploadId) {
  return request(`/uploads/${encodeURIComponent(uploadId)}`, {
    method: 'GET',
  });
}

export function listFiles() {
  return request('/files', { method: 'GET' });
}
