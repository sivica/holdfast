#!/bin/sh
set -eu
data="${HLGUARD_DATA_DIR:-/app/data}"
mkdir -p "$data"
chown -R holdfast:holdfast "$data" || true
exec runuser -u holdfast -- "$@"
