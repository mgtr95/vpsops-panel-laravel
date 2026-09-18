# Adopt an existing Laravel app onto the panel template

Use this on a VPS that already runs Laravel with PHP-FPM, `artisan serve`, or a custom compose file. The panel **generates** production Docker. Repo Docker is ignored after the switch.

The switch **always** writes a local database dump to `{app}/.panel-backups/{timestamp}/` before it changes `.env` or starts the new stack. Host `storage/` (and `public/uploads` when present) is **not deleted**. The new stack **bind-mounts** those host folders (`./storage/app`, `./storage/logs`, `./public/uploads`, and `./database` for SQLite) so uploads and SQLite stay on disk for OneDrive and panel backups. If an old container still has files only in a named volume, they are copied onto the host first. A retry **clears the new database first**, then restores the dump, so a failed first attempt (PHP version, health, etc.) does not fail later with “already exists” / duplicate keys.

## In the panel

1. **Apps** → open the app
2. Confirm extras (MySQL/Postgres/SQLite/none, Redis, queue, scheduler, Mailpit, extra packages like `tesseract-ocr` / `imagick`)
3. Leave **Copy existing data into the new database** on if you want the current data in the new container (MySQL → MySQL dump/restore, or any engine pair with conversion)
4. **Switch to panel template**
5. Wait for the pipeline: checkout → **database backup** → PHP → frontend → image → go live → **copy data** → migrate (skipped when the dump already loaded the schema) → health
6. nginx is rewritten to `proxy_pass http://{app-id}_app:80`
7. **Go live** if the domain vhost was still HTTP-only

To keep the current DB container instead of creating a new one, choose database **None**. The old database stays running; a dump is still written first.

Linking GitHub on a Laravel folder also marks it as panel-managed; the **next deploy** uses the template (backup/copy steps run only on a template switch, not on later deploys).

Scheduled **panel backup jobs** for that app’s old SQL connection are remapped to `{slug}-db` (`{slug}_postgres` or `{slug}_mysql`). Do not keep dumping the pre-switch hostname. See [AUDIT.md](AUDIT.md).

## Suggested order on a mixed VPS

1. Apps already on a custom image — swap `artisan serve :8000` for FrankenPHP `:80`
2. MariaDB/MySQL apps — panel MySQL with data copy
3. SQLite apps
4. Apps that need extra packages (`tesseract-ocr`) / extensions (`imagick`)
5. Remaining FPM apps
6. Apps already on the template — skip

After **every** public Laravel app on that VPS uses `{slug}_app:80`:

- Remove FastCGI `root` / `fastcgi_pass` vhosts (panel **Go live** already writes `proxy_pass`)
- Drop host bind-mounts of app source from nginx
- Use only the `vps_proxy` network (`nginx/docker-compose.proxy-only.yml` on this host)

Do not switch nginx to proxy-only while any live site still uses FastCGI or a non-standard port (for example `{slug}_app:8000`).
