#!/usr/bin/env bash
set -e

OPTIONS=/data/options.json
ADMIN_PORT=8000

if [ -f "$OPTIONS" ]; then
  value="$(jq -r '.admin_port // empty' "$OPTIONS")"
  if [ -n "$value" ]; then
    ADMIN_PORT="$value"
  fi
fi

export COMPANION_CONFIG_BASEDIR="${COMPANION_CONFIG_BASEDIR:-/config}"
export COMPANION_ADMIN_PORT="$ADMIN_PORT"
mkdir -p "$COMPANION_CONFIG_BASEDIR"

echo "Starting Bitfocus Companion on port ${ADMIN_PORT}, config in ${COMPANION_CONFIG_BASEDIR}"

# Arguments given here override Companion's own config.yaml, so the port set in
# the add-on configuration always wins.
exec /docker-entrypoint.sh --admin-port "$ADMIN_PORT"
