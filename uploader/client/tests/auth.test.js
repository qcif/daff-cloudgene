import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getToken, notSignedInMessage, redirectToLogin } from '../src/auth.js';

describe('getToken', () => {
  beforeEach(() => {
    window.localStorage.clear();
    import.meta.env.DEV = false;
    // Vite's import.meta.env coerces assigned values to strings (as real
    // env vars would be), so `= undefined` becomes the string "undefined"
    // rather than an absent value — clear it with `delete` instead.
    delete import.meta.env.VITE_DEV_TOKEN;
  });

  it('returns null when the key is absent', () => {
    expect(getToken()).toBeNull();
  });

  it('returns null when the value is unparseable JSON', () => {
    window.localStorage.setItem('cloudgene', 'not json{{{');
    expect(getToken()).toBeNull();
  });

  it('returns null when the value has no token key', () => {
    window.localStorage.setItem('cloudgene', JSON.stringify({ other: 'x' }));
    expect(getToken()).toBeNull();
  });

  it('returns null when token is present but empty', () => {
    window.localStorage.setItem('cloudgene', JSON.stringify({ token: '' }));
    expect(getToken()).toBeNull();
  });

  it('returns the token when present', () => {
    window.localStorage.setItem(
      'cloudgene', JSON.stringify({ token: 'abc123' }));
    expect(getToken()).toBe('abc123');
  });

  it('never reads a cached value: two calls see two different tokens', () => {
    window.localStorage.setItem('cloudgene', JSON.stringify({ token: 'first' }));
    expect(getToken()).toBe('first');
    window.localStorage.setItem('cloudgene', JSON.stringify({ token: 'second' }));
    expect(getToken()).toBe('second');
  });
});

describe('getToken dev escape hatch', () => {
  const originalDev = import.meta.env.DEV;
  const originalToken = import.meta.env.VITE_DEV_TOKEN;

  afterEach(() => {
    import.meta.env.DEV = originalDev;
    if (originalToken === undefined) {
      delete import.meta.env.VITE_DEV_TOKEN;
    } else {
      import.meta.env.VITE_DEV_TOKEN = originalToken;
    }
    window.localStorage.clear();
  });

  it('uses VITE_DEV_TOKEN when import.meta.env.DEV is set', () => {
    import.meta.env.DEV = true;
    import.meta.env.VITE_DEV_TOKEN = 'dev-token';
    expect(getToken()).toBe('dev-token');
  });

  it('falls back to localStorage when DEV but no VITE_DEV_TOKEN is set', () => {
    import.meta.env.DEV = true;
    delete import.meta.env.VITE_DEV_TOKEN;
    window.localStorage.setItem('cloudgene', JSON.stringify({ token: 'stored' }));
    expect(getToken()).toBe('stored');
  });

  it('never consults VITE_DEV_TOKEN when DEV is false', () => {
    import.meta.env.DEV = false;
    import.meta.env.VITE_DEV_TOKEN = 'dev-token';
    window.localStorage.setItem('cloudgene', JSON.stringify({ token: 'stored' }));
    expect(getToken()).toBe('stored');
  });
});

const ORIGIN = 'https://cloudgene.qcif.edu.au';

describe('redirectToLogin', () => {
  beforeEach(() => {
    import.meta.env.DEV = false;
    window.sessionStorage.clear();
    delete window.location;
    window.location = { href: `${ORIGIN}/uploads/` };
  });

  it('navigates to the login route with a return URL', () => {
    expect(redirectToLogin()).toBe(true);
    expect(window.location.href).toContain('return=');
    expect(window.location.href).toContain('%2Fuploads%2F');
    expect(window.location.href).toContain('#!pages/login');
  });

  it('never nests a return URL inside the next one', () => {
    // The shape that caused the runaway loop: the app is reached at a URL
    // that already carries a return param, and re-encodes the whole thing.
    window.location.href = `${ORIGIN}/uploads/?return=https%3A%2F%2Fx%2Fa`;

    redirectToLogin();

    // The code assigns a root-relative href, which a real browser resolves
    // against the origin; the stub here keeps it relative, so supply a base.
    const returned = decodeURIComponent(
      new URL(window.location.href, ORIGIN).searchParams.get('return'));
    expect(returned).not.toContain('return=');
    expect(returned).toBe(`${ORIGIN}/uploads/`);
  });

  it('redirects at most once per session, so a bounce cannot loop', () => {
    expect(redirectToLogin()).toBe(true);
    const afterFirst = window.location.href;

    // Second attempt, still with no token: must refuse rather than bounce.
    expect(redirectToLogin()).toBe(false);
    expect(window.location.href).toBe(afterFirst);
  });

  it('releases the guard once a token is found, so expiry can redirect again', () => {
    expect(redirectToLogin()).toBe(true);
    expect(redirectToLogin()).toBe(false);

    window.localStorage.setItem('cloudgene', JSON.stringify({ token: 'ok' }));
    expect(getToken()).toBe('ok');
    window.localStorage.clear();

    expect(redirectToLogin()).toBe(true);
  });

  it('does not navigate at all in dev, where "/" is the same Vite app', () => {
    import.meta.env.DEV = true;
    delete import.meta.env.VITE_DEV_TOKEN;
    const before = window.location.href;

    expect(redirectToLogin()).toBe(false);
    expect(window.location.href).toBe(before);
  });
});

describe('notSignedInMessage', () => {
  it('tells a dev to set VITE_DEV_TOKEN', () => {
    import.meta.env.DEV = true;
    expect(notSignedInMessage()).toContain('VITE_DEV_TOKEN');
  });

  it('tells a real user to sign in to Cloudgene', () => {
    import.meta.env.DEV = false;
    expect(notSignedInMessage()).toContain('Cloudgene');
    expect(notSignedInMessage()).not.toContain('VITE_DEV_TOKEN');
  });
});
