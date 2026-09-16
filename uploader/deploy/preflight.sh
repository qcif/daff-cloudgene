#!/usr/bin/env bash
# preflight.sh — checks the host is ready for the uploader. Changes nothing.
#
# Usage:
#     ./preflight.sh /mnt/data/daff-cloudgene
#
# The single argument is REPO_ROOT — the checkout path settled once in
# deploy/README.md §0. Every check below is read-only. Exits 0 if every
# check passes, 1 otherwise; install.sh refuses to run unless this exits 0.
set -u

REPO_ROOT="${1:-}"
STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-daffstandard}"
STORAGE_CONTAINER="${AZURE_STORAGE_CONTAINER:-uploads}"
ORIGIN="https://cloudgene.qcif.edu.au"
UPLOADS_PORT=8003

fail=0

ok() { printf '  OK   %s\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*"; fail=1; }
info() { printf '  INFO %s\n' "$*"; }

echo "== Checkout =="
if [ -z "$REPO_ROOT" ]; then
    bad "no REPO_ROOT given — usage: ./preflight.sh /path/to/checkout"
else
    if [ ! -d "$REPO_ROOT" ]; then
        bad "$REPO_ROOT does not exist"
    elif [ ! -d "$REPO_ROOT/.git" ]; then
        bad "$REPO_ROOT is not a git checkout (no .git)"
    elif [ ! -f "$REPO_ROOT/uploader/api/app.py" ] \
        || [ ! -f "$REPO_ROOT/uploader/deploy/README.md" ]; then
        bad "$REPO_ROOT does not look like this repo (missing" \
            "uploader/api/app.py or uploader/deploy/README.md) — note" \
            "config/cloudgene.service lives in a different tree" \
            "(/mnt/data/cloudgene), not this one"
    else
        ok "$REPO_ROOT is a checkout of this repo"
    fi

    if [ -f "$REPO_ROOT/uploader/client/dist/index.html" ]; then
        ok "uploader/client/dist/index.html present (SPA is built)"
    else
        bad "uploader/client/dist/index.html missing — the SPA has not" \
            "been built, or dist/ was not committed/pulled"
    fi
fi

echo "== Python venv =="
if [ -n "$REPO_ROOT" ] && [ -x "$REPO_ROOT/uploader/venv/bin/uvicorn" ]; then
    ver=$("$REPO_ROOT/uploader/venv/bin/python3" --version 2>&1)
    ok "venv present, uvicorn executable found ($ver)"
else
    bad "no venv at \$REPO_ROOT/uploader/venv with bin/uvicorn — build it:" \
        "python3 -m venv $REPO_ROOT/uploader/venv && " \
        "$REPO_ROOT/uploader/venv/bin/pip install -r " \
        "$REPO_ROOT/uploader/api/requirements.txt"
fi

echo "== Port 8003 =="
if command -v ss >/dev/null 2>&1; then
    if ss -lntp 2>/dev/null | grep -q ":${UPLOADS_PORT} "; then
        bad "something is already listening on 127.0.0.1:${UPLOADS_PORT}"
    else
        ok "port ${UPLOADS_PORT} is free"
    fi
else
    info "ss not found — cannot check port ${UPLOADS_PORT}, check manually"
fi

echo "== DNS resolver (RestrictAddressFamilies / AF_UNIX) =="
# glibc NSS resolves hostnames one of two ways: the "dns" module talks
# UDP/TCP directly to a nameserver (AF_INET/AF_INET6 is enough), the
# "resolve" module hands the lookup to systemd-resolved over a unix
# socket (AF_UNIX is required too). /etc/resolv.conf pointing at the
# 127.0.0.53 stub does NOT by itself imply the unix-socket path — only the
# NSS module in use decides that.
if [ -f /etc/nsswitch.conf ]; then
    hosts_line=$(grep -E '^hosts:' /etc/nsswitch.conf || true)
    echo "  $hosts_line"
    if echo "$hosts_line" | grep -qw resolve; then
        bad "NSS uses the 'resolve' module — add AF_UNIX to" \
            "RestrictAddressFamilies in uploads.service before starting it," \
            "or outbound HTTPS to login.microsoftonline.com will fail with" \
            "a name-resolution error that looks like a network problem"
    elif echo "$hosts_line" | grep -qw dns; then
        ok "NSS uses the 'dns' module — AF_INET/AF_INET6 alone is sufficient"
    else
        info "could not identify the NSS hosts module from nsswitch.conf" \
            "— inspect the line above manually"
    fi
else
    info "/etc/nsswitch.conf not found — cannot determine the NSS module"
fi

echo "== Egress to Azure =="
if command -v curl >/dev/null 2>&1; then
    login_code=$(curl -sS -o /dev/null -w '%{http_code}' -m 10 \
        https://login.microsoftonline.com/common/discovery/keys || echo000)
    if [ "$login_code" = "200" ]; then
        ok "login.microsoftonline.com reachable (200)"
    else
        bad "login.microsoftonline.com returned '$login_code', expected 200"
    fi

    blob_code=$(curl -sS -o /dev/null -w '%{http_code}' -m 10 \
        "https://${STORAGE_ACCOUNT}.blob.core.windows.net/" || echo 000)
    if [ "$blob_code" = "400" ]; then
        ok "${STORAGE_ACCOUNT}.blob.core.windows.net reachable" \
            "(400, expected for an unauthenticated request)"
    else
        bad "${STORAGE_ACCOUNT}.blob.core.windows.net returned" \
            "'$blob_code', expected 400"
    fi
else
    info "curl not found — cannot check egress"
fi

echo "== CORS on the storage account (Task 2 §4) =="
if command -v curl >/dev/null 2>&1; then
    cors_headers=$(curl -sS -i -X OPTIONS -m 10 \
        "https://${STORAGE_ACCOUNT}.blob.core.windows.net/${STORAGE_CONTAINER}/preflight-probe.txt" \
        -H "Origin: ${ORIGIN}" \
        -H "Access-Control-Request-Method: PUT" \
        -H "Access-Control-Request-Headers: x-ms-blob-type,x-ms-blob-content-type,content-type,content-length,x-ms-version,x-ms-client-request-id,x-ms-useragent" \
        2>/dev/null)
    if echo "$cors_headers" | grep -qi '^access-control-allow-origin:'; then
        ok "CORS preflight succeeded — the seven-header rule is applied"
    else
        bad "CORS preflight did not return access-control-allow-origin —" \
            "the rule may have been cleared, edited or never applied" \
            "(Task 2 §4). This is the single most likely cause of a" \
            "deployment that installs cleanly and does not work."
    fi
else
    info "curl not found — cannot check CORS"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "preflight: all checks passed"
else
    echo "preflight: FAILED — fix the items above before installing"
fi
exit "$fail"
