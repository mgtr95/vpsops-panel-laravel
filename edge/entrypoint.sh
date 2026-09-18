#!/bin/sh
# Generate Caddyfile (Portainer-style access):
# - No PANEL_DOMAIN                         → http://SERVER_IP:9090
# - PANEL_DOMAIN + PANEL_AUTO_HTTPS=true    → https://domain (LE + renew on 80/443)
# - PANEL_DOMAIN + PANEL_AUTO_HTTPS=false   → http://IP:9090 (TLS handled by external proxy)
set -e

DOMAIN="${PANEL_DOMAIN:-}"
EMAIL="${ACME_EMAIL:-}"
AUTO_HTTPS="${PANEL_AUTO_HTTPS:-}"
UPSTREAM="${UPSTREAM:-dashboard:9090}"
OUT="${CADDYFILE:-/etc/caddy/Caddyfile}"

# Default: auto HTTPS when a domain is set
if [ -z "$AUTO_HTTPS" ]; then
  if [ -n "$DOMAIN" ]; then
    AUTO_HTTPS=true
  else
    AUTO_HTTPS=false
  fi
fi

mkdir -p /data/caddy /etc/caddy

if [ -n "$DOMAIN" ] && [ "$AUTO_HTTPS" = "true" ]; then
  if [ -z "$EMAIL" ]; then
    echo "PANEL_AUTO_HTTPS=true requires ACME_EMAIL for Let's Encrypt." >&2
    exit 1
  fi
  cat > "$OUT" <<EOF
{
  email ${EMAIL}
}

${DOMAIN} {
  encode gzip
  reverse_proxy ${UPSTREAM} {
    header_up X-Forwarded-Proto {scheme}
    header_up X-Real-IP {remote_host}
  }
}

:9090 {
  redir https://${DOMAIN}{uri} permanent
}
EOF
  echo "Caddy: automatic HTTPS for ${DOMAIN} (publish host ports 80 + 443)"
elif [ -n "$DOMAIN" ]; then
  cat > "$OUT" <<EOF
{
  auto_https off
}

:9090 {
  encode gzip
  reverse_proxy ${UPSTREAM} {
    header_up X-Forwarded-Proto {scheme}
    header_up X-Real-IP {remote_host}
  }
}
EOF
  echo "Caddy: HTTP :9090 (domain ${DOMAIN} — TLS expected from external proxy)"
else
  cat > "$OUT" <<EOF
{
  auto_https off
}

:9090 {
  encode gzip
  reverse_proxy ${UPSTREAM} {
    header_up X-Forwarded-Proto {scheme}
    header_up X-Real-IP {remote_host}
  }
}
EOF
  echo "Caddy: HTTP only on :9090 — open http://SERVER_IP:9090"
fi

exec caddy run --config "$OUT" --adapter caddyfile
