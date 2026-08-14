#!/bin/sh
set -eu

mkdir -p "${STATE_DIR:-/data/state}" "${GPX_DIR:-/data/gpx}" "${LOG_DIR:-/data/logs}"

# Requests normally uses certifi's public roots. When a homelab CA is mounted,
# append it instead of replacing the public bundle (Strava still needs it).
CERTIFI_BUNDLE="$(python -m certifi)"
if [ -f "${CUSTOM_CA_CERT:-/certs/rootCA.pem}" ]; then
    cat "$CERTIFI_BUNDLE" "${CUSTOM_CA_CERT:-/certs/rootCA.pem}" > /tmp/combined-ca.pem
else
    cp "$CERTIFI_BUNDLE" /tmp/combined-ca.pem
fi

exec "$@"
