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

# --- Home Assistant ingress ---------------------------------------------------
# The Supervisor picks a free port for ingress (the add-on is on the host
# network). Ask it which one, then proxy that port to Companion.
INGRESS_PORT="${INGRESS_PORT:-}"
if [ -z "$INGRESS_PORT" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
  INGRESS_PORT="$(curl -fsS -m 10 -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    http://supervisor/addons/self/info | jq -r '.data.ingress_port // empty' || true)"
fi

if [[ "$INGRESS_PORT" =~ ^[0-9]+$ ]] && [ "$INGRESS_PORT" -gt 0 ]; then
  sed -e "s/%%INGRESS_PORT%%/${INGRESS_PORT}/" \
      -e "s/%%ADMIN_PORT%%/${ADMIN_PORT}/" \
      -e "s|%%INGRESS_ALLOW%%|${INGRESS_ALLOW:-172.30.32.2}|" \
      /etc/nginx/companion-ingress.conf.template > /tmp/companion-ingress.conf
  if nginx -t -q -c /tmp/companion-ingress.conf; then
    nginx -c /tmp/companion-ingress.conf -g 'daemon off;' &
    echo "Ingress proxy listening on port ${INGRESS_PORT}"
  else
    echo "Ingress proxy configuration is invalid; continuing without ingress"
  fi
else
  echo "No ingress port available; continuing without ingress"
fi

echo "Starting Bitfocus Companion on port ${ADMIN_PORT}, config in ${COMPANION_CONFIG_BASEDIR}"

# Arguments given here override Companion's own config.yaml, so the port set in
# the add-on configuration always wins.
exec /docker-entrypoint.sh --admin-port "$ADMIN_PORT"
