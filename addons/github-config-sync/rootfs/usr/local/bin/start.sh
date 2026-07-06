#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "Starting Github Config Sync add-on service"
exec python3 /app/server.py
