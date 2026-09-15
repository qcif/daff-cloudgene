// Reads the Cloudgene session token from localStorage, per §01-api.md §4 /
// 02-client.md §5. Same-origin placement is what makes this possible at
// all: `localStorage` is partitioned by origin, so /uploads/ can only read
// Cloudgene's token because it shares cloudgene.qcif.edu.au.
//
// The token is never cached in a module variable — a renewal in another
// Cloudgene tab must be picked up on the next call, not produce a spurious
// logout — and the JWT payload is never parsed: only Cloudgene's own answer
// about a token counts (see uploader/api/cloudgene_auth.py).

const STORAGE_KEY = 'cloudgene';
const RETURN_PARAM = 'return';
const LOGIN_ROUTE = '#!pages/login';
const REDIRECT_GUARD_KEY = 'uploader.redirected-to-login';

// Dev-only escape hatch: the Vite dev server is a different origin from
// Cloudgene, so there is no token in localStorage there. import.meta.env.DEV
// is statically replaced by Vite at build time (false in a production
// build), so this branch is dead code — and therefore stripped by
// tree-shaking/minification — once built. Verified by
// tests/auth.build.test.js, which greps the built bundle for the env var
// name.
function devToken() {
  if (import.meta.env.DEV) {
    return import.meta.env.VITE_DEV_TOKEN || null;
  }
  return null;
}

// Returns the current token, or null if the user is not signed in to
// Cloudgene (key absent, unparseable JSON, or no `token` property).
export function getToken() {
  const dev = devToken();
  if (dev) return dev;

  let raw;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }

  if (!raw) return null;

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  if (!parsed || typeof parsed.token !== 'string' || !parsed.token) {
    return null;
  }

  // A usable token means any earlier redirect did its job; release the
  // one-shot guard so a later expiry can redirect again.
  clearRedirectGuard();

  return parsed.token;
}

function clearRedirectGuard() {
  try {
    window.sessionStorage.removeItem(REDIRECT_GUARD_KEY);
  } catch {
    // sessionStorage unavailable; nothing to release.
  }
}

export const NOT_SIGNED_IN_MESSAGE = (
  'Not signed in to Cloudgene. Sign in, then reload this page.');

// The message to show when the API cannot be called because there is no
// usable session and no redirect was performed.
//
// The dev text is written inline inside the DEV branch rather than hoisted
// to a module constant on purpose: Vite replaces import.meta.env.DEV with
// `false` at build time, so an inline literal is dropped with the dead
// branch. A module-level constant would survive into the production bundle
// and trip tests/auth.build.test.js — which is exactly what it is for.
export function notSignedInMessage() {
  if (import.meta.env.DEV) {
    return 'No Cloudgene token. The Vite dev server is a different origin '
      + 'from Cloudgene, so localStorage holds no token here — put '
      + 'VITE_DEV_TOKEN in uploader/client/.env.local and restart Vite '
      + '(see uploader/README.md).';
  }
  return NOT_SIGNED_IN_MESSAGE;
}

// Returns the current URL with any existing `return` parameter removed, so
// that a redirect which bounces straight back cannot nest one return URL
// inside the next. Without this, each bounce re-encodes the whole previous
// URL and the query string grows exponentially until the server rejects the
// request headers outright.
function currentUrlWithoutReturn() {
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete(RETURN_PARAM);
    return url.toString();
  } catch {
    return window.location.href;
  }
}

// Sends the browser to the Cloudgene login with a return URL back to the
// current page. Cloudgene is a hash-routed SPA at the origin root; its login
// view lives at the '#!pages/login' route. Cloudgene's own login flow does
// not currently read a return URL (it always lands back on '/' after
// success), but the query param is still attached ahead of the hash — it
// costs nothing, and picks up for free if that ever changes.
//
// Returns whether the browser is actually navigating away. A caller that
// gets `false` must surface notSignedInMessage() itself, because nothing
// else is going to happen.
export function redirectToLogin() {
  // In dev there is no Cloudgene on this origin: '/' is Vite's own SPA
  // fallback, which serves this same app straight back. Redirecting would
  // bounce between the app and itself forever. The dev path is
  // VITE_DEV_TOKEN, so say that instead of navigating.
  if (import.meta.env.DEV) {
    console.error(`[uploader] ${notSignedInMessage()}`);
    return false;
  }

  // Redirect at most once per tab session. If we land back here still
  // without a token, the login did not take, and bouncing again would loop
  // forever rather than ever showing the user what went wrong.
  try {
    if (window.sessionStorage.getItem(REDIRECT_GUARD_KEY)) {
      console.error(
        '[uploader] Already redirected to the Cloudgene login once in this '
        + 'session and still have no token; not redirecting again.');
      return false;
    }
    window.sessionStorage.setItem(REDIRECT_GUARD_KEY, '1');
  } catch {
    // sessionStorage unavailable (private mode, disabled storage). Proceed
    // unguarded rather than blocking a legitimate login.
  }

  const returnUrl = encodeURIComponent(currentUrlWithoutReturn());
  window.location.href = `/?${RETURN_PARAM}=${returnUrl}${LOGIN_ROUTE}`;
  return true;
}

// Returns the token, or redirects to login and returns null if there is
// none. Callers must not proceed to call the API when this returns null.
export function requireToken() {
  const token = getToken();
  if (!token) {
    redirectToLogin();
    return null;
  }
  return token;
}
