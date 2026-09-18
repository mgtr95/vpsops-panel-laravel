#!/usr/bin/env bash
# Optional helper if you terminate TLS on an *external* nginx (not Caddy auto-HTTPS).
# Prefer: PANEL_AUTO_HTTPS=true + docker-compose.https.yml when ports 80/443 are free.
set -euo pipefail

DOMAIN="${PANEL_DOMAIN:-}"
EMAIL="${CERTBOT_EMAIL:-${ACME_EMAIL:-}}"
CONF_D="${NGINX_CONF_D:-}"

if [[ -z "$DOMAIN" ]]; then
  echo "Set PANEL_DOMAIN to your real hostname (example: panel.example.com)"
  exit 1
fi
if [[ -z "$EMAIL" ]]; then
  echo "Set CERTBOT_EMAIL=you@example.com (or ACME_EMAIL)"
  exit 1
fi
if [[ -z "$CONF_D" ]]; then
  echo "Set NGINX_CONF_D=/path/to/nginx/conf.d"
  exit 1
fi

IP="$(curl -4 -fsS ifconfig.me || true)"
echo "DNS A record required:  $DOMAIN  ->  ${IP:-YOUR_VPS_IP}"
read -r -p "Press Enter when DNS is ready…"

if [[ -z "${CERTBOT_CONTAINER:-}" ]]; then
  echo "Set CERTBOT_CONTAINER to the certbot container name"
  exit 1
fi
if [[ -z "${NGINX_CONTAINER:-}" ]]; then
  echo "Set NGINX_CONTAINER to the nginx container name"
  exit 1
fi

docker exec "$CERTBOT_CONTAINER" certbot certonly \
  --webroot -w /var/www/certbot \
  -d "$DOMAIN" \
  --agree-tos -m "$EMAIL" \
  --non-interactive \
  --keep-until-expiring

cat > "$CONF_D/vps-dashboard.conf" <<EOF
upstream vps_dashboard {
    server vps_dashboard:9090;
    keepalive 4;
}

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://${DOMAIN}\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    client_max_body_size 64m;

    location / {
        proxy_pass http://vps_dashboard;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection        "";
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }
}
EOF

docker exec "$NGINX_CONTAINER" nginx -t
docker exec "$NGINX_CONTAINER" nginx -s reload
echo "HTTPS ready: https://${DOMAIN}"
