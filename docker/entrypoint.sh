#!/bin/sh
set -eu

# Merge an optional customer/bank public CA into the container trust bundle.
# The project directory ./certs is mounted read-only at /certs.
if [ -n "${CUSTOM_CA_FILE:-}" ]; then
  CA_NAME="$(basename "$CUSTOM_CA_FILE")"
  CA_SOURCE="/certs/$CA_NAME"
  if [ -f "$CA_SOURCE" ]; then
    mkdir -p "$(dirname "${CUSTOM_CA_CONTAINER_FILE:-/run/oraiflow/custom-ca.crt}")"
    cp "$CA_SOURCE" "${CUSTOM_CA_CONTAINER_FILE:-/run/oraiflow/custom-ca.crt}"
    BASE_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
    TARGET_BUNDLE="${CA_BUNDLE_PATH:-/run/oraiflow/ca-bundle.pem}"
    cat "$BASE_BUNDLE" "${CUSTOM_CA_CONTAINER_FILE:-/run/oraiflow/custom-ca.crt}" > "$TARGET_BUNDLE"
    export SSL_CERT_FILE="$TARGET_BUNDLE"
    export REQUESTS_CA_BUNDLE="$TARGET_BUNDLE"
    export CURL_CA_BUNDLE="$TARGET_BUNDLE"
    echo "[oraiflow] Custom CA loaded from $CA_SOURCE"
  else
    echo "[oraiflow] Custom CA not present at $CA_SOURCE; using system trust store"
    export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"
    export REQUESTS_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
  fi
fi

exec "$@"
