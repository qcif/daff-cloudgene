import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/auth.js', () => ({
  getToken: vi.fn(),
  // Returns whether the browser is navigating away; the default here is the
  // production case, where it is.
  redirectToLogin: vi.fn(() => true),
  notSignedInMessage: vi.fn(() => 'Set VITE_DEV_TOKEN'),
}));

import { getToken, redirectToLogin } from '../src/auth.js';
import { ApiError, AUTH_REDIRECT, createUpload, listFiles } from '../src/api.js';

function jsonResponse(status, body, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: (name) => headers[name] ?? null },
    json: async () => body,
  };
}

describe('api request wrapper', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    getToken.mockReturnValue('a-token');
    redirectToLogin.mockReturnValue(true);
    global.fetch = vi.fn();
  });

  it('attaches X-Auth-Token on every call', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { files: [], truncated: false }));
    await listFiles();
    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers['X-Auth-Token']).toBe('a-token');
  });

  it('redirects to login and throws without calling fetch when no token', async () => {
    getToken.mockReturnValue(null);
    await expect(listFiles()).rejects.toMatchObject({ status: AUTH_REDIRECT });
    expect(redirectToLogin).toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('redirects to login on 401', async () => {
    global.fetch.mockResolvedValue(jsonResponse(401, { detail: 'nope' }));
    await expect(listFiles()).rejects.toMatchObject({ status: AUTH_REDIRECT });
    expect(redirectToLogin).toHaveBeenCalled();
  });

  it('surfaces a displayable 401 when no redirect happens (dev)', async () => {
    // The dev case: redirectToLogin declines to navigate, so the error must
    // be shown rather than swallowed as a page that is going away.
    getToken.mockReturnValue(null);
    redirectToLogin.mockReturnValue(false);

    await expect(listFiles()).rejects.toMatchObject({
      status: 401,
      detail: 'Set VITE_DEV_TOKEN',
    });
  });

  it('surfaces a displayable 401 when a 401 response does not redirect', async () => {
    redirectToLogin.mockReturnValue(false);
    global.fetch.mockResolvedValue(jsonResponse(401, { detail: 'expired' }));

    await expect(listFiles()).rejects.toMatchObject({ status: 401 });
  });

  it('surfaces 403 detail verbatim without redirecting', async () => {
    getToken.mockReturnValue('a-token');
    global.fetch.mockResolvedValue(
      jsonResponse(403, { detail: 'No workflow access' }));
    await expect(listFiles()).rejects.toMatchObject({
      status: 403,
      detail: 'No workflow access',
    });
    expect(redirectToLogin).not.toHaveBeenCalled();
  });

  it('surfaces 400 detail naming the failing check', async () => {
    global.fetch.mockResolvedValue(jsonResponse(400, { detail: 'bad size' }));
    await expect(createUpload({ filename: 'a.csv', size: 1, contentType: 'text/csv' }))
      .rejects.toMatchObject({ status: 400, detail: 'bad size' });
  });

  it('is terminal on 404', async () => {
    global.fetch.mockResolvedValue(jsonResponse(404, { detail: 'No such upload' }));
    await expect(listFiles()).rejects.toMatchObject({ status: 404 });
  });

  it('is terminal on 409', async () => {
    global.fetch.mockResolvedValue(jsonResponse(409, { detail: 'terminal' }));
    await expect(listFiles()).rejects.toMatchObject({ status: 409 });
  });

  it('honours Retry-After on 429', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(429, { detail: 'slow down' }, { 'Retry-After': '30' }));
    await expect(listFiles()).rejects.toMatchObject({
      status: 429,
      retryAfterSeconds: 30,
    });
  });

  it('maps 503 to a generic try-again message, not the raw detail', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(503, { detail: 'internal database is on fire' }));
    await expect(listFiles()).rejects.toMatchObject({
      status: 503,
      detail: 'Try again shortly.',
    });
  });

  it('maps a network failure to 503', async () => {
    global.fetch.mockRejectedValue(new TypeError('network down'));
    await expect(listFiles()).rejects.toMatchObject({ status: 503 });
  });
});
