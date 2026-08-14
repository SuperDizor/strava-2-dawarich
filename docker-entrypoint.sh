#!/bin/sh
set -eu

mkdir -p "${STATE_DIR:-/data/state}" "${GPX_DIR:-/data/gpx}" "${LOG_DIR:-/data/logs}"
exec "$@"
