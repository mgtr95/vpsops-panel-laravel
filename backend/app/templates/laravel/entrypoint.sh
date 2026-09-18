#!/bin/sh
set -e
cd /app

mkdir -p storage/framework/cache storage/framework/sessions storage/framework/views storage/logs bootstrap/cache

if [ -z "${APP_KEY}" ]; then
    php artisan key:generate --force --no-interaction
fi

if [ "${DB_CONNECTION:-}" = "sqlite" ]; then
    DB_FILE="${DB_DATABASE:-/app/database/database.sqlite}"
    mkdir -p "$(dirname "$DB_FILE")"
    [ -f "$DB_FILE" ] || touch "$DB_FILE"
fi

php artisan storage:link --force >/dev/null 2>&1 || true

# Vite/asset() follow ASSET_URL. Behind nginx TLS, APP_URL is https but the
# request to FrankenPHP is HTTP, so an empty ASSET_URL would still emit http://.
if [ -z "${ASSET_URL:-}" ] && [ -n "${APP_URL:-}" ]; then
    case "$APP_URL" in
        https://*) export ASSET_URL="$APP_URL" ;;
    esac
fi

if [ "$1" = "frankenphp" ]; then
    php artisan config:cache >/dev/null 2>&1 || true
    php artisan route:cache >/dev/null 2>&1 || true
    php artisan view:cache >/dev/null 2>&1 || true
    chown -R www-data:www-data storage bootstrap/cache database 2>/dev/null || true
fi

exec "$@"
