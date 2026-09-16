#!/usr/bin/env bash
# install.sh — first-deploy convenience script for the Azure uploader.
#
# Every step here is also written out longhand in deploy/README.md. This
# script is a convenience, never the only path: if it fails partway or
# behaves unexpectedly, read the README's "First deploy" section and run
# the remaining steps by hand.
#
# Strictly idempotent and never a silent overwrite: if /etc/uploads.env,
# the certificate, or the unit already exist, this reports and leaves them
# alone rather than touching them.
#
# Refuses to run if preflight.sh fails — a half-installed service is worse
# than an uninstalled one.
#
# Usage:
#     sudo ./install.sh /mnt/data/daff-cloudgene /path/to/azure-cert.pem
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:?usage: install.sh REPO_ROOT CERT_PATH}"
CERT_SRC="${2:?usage: install.sh REPO_ROOT CERT_PATH}"

ENV_FILE=/etc/uploads.env
CERT_DIR=/etc/cloudgene-uploader
CERT_DEST="${CERT_DIR}/azure-cert.pem"
UNIT_DEST=/etc/systemd/system/uploads.service

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root (it installs to /etc and" \
        "/etc/systemd/system)" >&2
    exit 1
fi

echo "Running preflight.sh ${REPO_ROOT} ..."
if ! "${SCRIPT_DIR}/preflight.sh" "${REPO_ROOT}"; then
    echo "preflight failed — not installing anything. Fix the items above" \
        "and re-run." >&2
    exit 1
fi

if [ -e "$ENV_FILE" ]; then
    echo "SKIP: $ENV_FILE already exists — leaving it alone. Confirm its" \
        "contents match deploy/uploads.env.sample before continuing."
else
    install -o root -g www-data -m 0640 /dev/null "$ENV_FILE"
    echo "Created empty $ENV_FILE (0640 root:www-data)." \
        "Fill it in from deploy/uploads.env.sample before starting the" \
        "service — it has no Azure credentials yet."
fi

if [ -e "$CERT_DEST" ]; then
    echo "SKIP: $CERT_DEST already exists — leaving it alone."
else
    install -d -o root -g root -m 0755 "$CERT_DIR"
    install -o www-data -g www-data -m 0600 "$CERT_SRC" "$CERT_DEST"
    echo "Installed certificate to $CERT_DEST (0600 www-data:www-data)."
fi

if [ -e "$UNIT_DEST" ]; then
    echo "SKIP: $UNIT_DEST already exists — leaving it alone. If you are" \
        "upgrading the unit file itself, that is a deliberate edit, not" \
        "something this script does for you."
else
    sed "s|@REPO_ROOT@|${REPO_ROOT}|g" "${SCRIPT_DIR}/uploads.service" \
        > "$UNIT_DEST"
    chmod 0644 "$UNIT_DEST"
    echo "Installed $UNIT_DEST with REPO_ROOT=${REPO_ROOT} substituted."
    systemctl daemon-reload
    echo "Ran systemctl daemon-reload."
fi

cat <<EOF

Not done automatically, on purpose — each needs a manual confirmation:

  1. Fill in $ENV_FILE with the three Azure credentials (see
     deploy/uploads.env.sample).
  2. Paste deploy/nginx-uploads.conf (with @REPO_ROOT@ substituted) into
     the vhost, above the Cloudgene location block.
  3. sudo nginx -t && sudo systemctl reload nginx
  4. sudo systemctl enable --now uploads
  5. Run the verification steps in deploy/README.md §9.
EOF
