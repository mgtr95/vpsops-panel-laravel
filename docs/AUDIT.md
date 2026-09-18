# VPSops audit log

Living list of dash bugs and area changes that were already diagnosed and
fixed. **Read this before changing the area a new change touches. Always append
a dated entry when you change that area.**

Covered so far: container alerts, log matching, queue worker command, Postgres
healthcheck, template-switch database connections, scheduled backups, app-file
backup folder selection, mail test send, grouped backup manual-run mail, rclone
OneDrive wizard, Laravel storage named volumes after template switch,
SQLite named volume vs host file backups, shared-proxy DB/redis DNS collisions,
per-app PHP upload limits via `.panel/php` bind mount.

Point an agent at this file:

> Read `vps-ops-panel/docs/AUDIT.md` before you change that area. Append a dated entry when you fix a dash bug **or change that area**. Do not reintroduce those fixes.

---

## 2026-09-18 — Per-app PHP upload limits (panel template)

**Symptom.** Panel-managed FrankenPHP apps kept PHP defaults (`upload_max_filesize=2M`,
`post_max_size=8M`). App UI / Laravel validation allowed 20 MB; nginx was 32m. Uploads
around 8.5 MB failed. Old per-app `docker/conf.d/uploads.ini` was never applied after
switching to `Dockerfile.panel`.

**Cause.** Panel template image did not ship a custom PHP ini. Limits were not editable
per app in the panel.

**Fix.**
- Store `php` limits on each app in `apps.json` (upload/post/memory/execution/input +
  nginx `client_max_body_size`).
- Write `.panel/php/zz-panel.ini` and bind-mount it into app/queue/scheduler at
  `/usr/local/etc/php/conf.d/zz-panel.ini`.
- App page **PHP & uploads** → `PUT /apps/{id}/php` syncs ini + compose, `compose up -d`
  (no rebuild), rewrites nginx body size when a domain conf exists.
- Defaults: 20M / 30M / 512M / 120s / nginx max(post, 32)M.
- `.dockerignore` excludes `.panel/php` so the ini is not baked into the image;
  Caddyfile/entrypoint under `.panel/` stay copyable for the Dockerfile.

**Deploy / restart durability (do not regress).**
- **Deploy:** After `git checkout --force` / `reset --hard`, pipeline loads `app["php"]`
  from `apps.json`, `normalize_php` (preserves custom values; fills defaults if missing),
  then `write_runtime(..., php_limits=app["php"])` rewrites `zz-panel.ini` + compose
  (mount included) **before** image build and `compose up`. Untracked `.panel/` is not
  removed by hard reset; even if the ini file were deleted, the next deploy recreates
  it from `apps.json`. End of pipeline `_refresh_vhost` rewrites nginx body size from
  the same `php` block.
- **Restart only:** `docker restart` keeps the bind mount and host ini; limits unchanged.
- **Save without rebuild:** `sync_php_runtime` + `compose up -d --no-build` is enough.

**Do not.** Bake upload limits only into the image (requires rebuild to change). Do not
drop the `.panel/php` mount from compose. Do not set nginx body size below `post_max_size`.
Do not `git clean -fdx` the app tree in checkout (would wipe `.panel/`). Do not dockerignore
all of `.panel/` (breaks `COPY .panel/Caddyfile` / entrypoint).

**Tests.** `backend/tests/test_laravel_template.py` (normalize_php, ini write, compose
mount, nginx body size).

---

## 2026-09-16 — Shared proxy DNS: `postgres` / `mysql` / `redis` collide across apps

**Symptom.** New Laravel app `uz-latest-2fa` (panel **New Laravel App**) failed at migrate:
`password authentication failed for user "laravel"` while connecting to host
`postgres`. Containers were healthy. Queue sometimes worked; app/migrate often
did not. `getent hosts postgres` inside the app returned multiple IPs
(kpc-uz / pou-uz / uz-latest-2fa Postgres on `artmedia_web_network`).

**Cause.** Template put `postgres` / `mysql` / `redis` on both `proxy` (shared
nginx network) and `internal`. Compose publishes the short service name on every
attached network, so every app’s `DB_HOST=postgres` round-robins across foreign
databases with different passwords.

**Fix.** `generate_compose` attaches database and redis to **`internal` only**.
App stays on `proxy` + `internal` for nginx. Panel SQL access already uses
`{slug}_postgres` / `{slug}_mysql` and `connect_dashboard_to_peer` (joins the
app-private network). Do not put short-named DB/redis services back on `proxy`.

**Do not.** “Fix” this by changing only one app’s `DB_HOST`. Do not rely on
container_name uniqueness while the service alias `postgres` still exists on the
shared network.

**Tests.** `backend/tests/test_laravel_template.py` (`test_compose_mailpit_and_queue`).

---

## 2026-09-15 — SQLite stayed in a named volume after the storage remount

**Symptom.** Artmedia Web SQLite backup copied `/apps/artmedia-web/database/database.sqlite` on the host and reported success. The live file was `panel-artmedia-web_database` (`/app/database/database.sqlite`). Host mtime froze at template switch; hashes differed a few hours later.

**Cause.** Storage bind-mounts were added for `storage/app`, `storage/logs`, and `public/uploads`. SQLite still used named volume `database:/app/database`. Panel sqlite jobs `shutil.copy2` the host path, same as app-file backups.

**Fix.** Template bind-mounts `./database:/app/database`. Adopt copies the live SQLite file onto the host before remount. Do not keep a `database` named volume for Laravel SQLite. Postgres/MySQL data stays in named volumes.

**Do not.** Put SQLite back on `database:/app/database`. Do not `compose up` the bind mount until the volume has been copied onto the host. Do not treat a successful sqlite copy of the host file as proof it matches the live DB.

**Tests.** `backend/tests/test_laravel_template.py`.

---

## 2026-09-15 — Template switch hid uploads in a Docker volume

**Symptom.** Zuc Uz OneDrive backup of `storage/app/public/attachments` showed
16,540 items and could not find a file the app had served (`Dozvola.pdf` /
`1e40yF0x842B3n0N66Zw1NmVVHS0djGXcKtvJzGR.pdf`). The same path on the host
matched that count; newest host file was 10 Sep. The live container had 16,555
files including that PDF (uploaded 15 Sep).

**Cause.** Panel template compose used named volumes `storage:/app/storage/app`
and `logs:/app/storage/logs`. Adopt copied host storage into the volume once,
then new uploads wrote only to Docker (`panel-{slug}_storage`). Panel file
backups and OneDrive read `{app}/storage/` on disk, so they froze at switch
time. The next deploy would have rewritten compose the same way.

**Fix.**

- Compose bind-mounts `./storage/app`, `./storage/logs`, `./public/uploads`.
  SQLite bind-mounts `./database`. Postgres/MySQL data stays in named volumes.
  Storage stays out of the image (`.dockerignore`).
- Before rewriting compose, copy live container/volume files onto the host if
  the mount is not already that folder. Adopt copies old-container extras onto
  the host too, not into a new named volume.
- Remounted already-templated apps the same way so the next deploy does not
  cover live files with an empty/stale named volume.

**Do not.** Put Laravel `storage/app` back on a named Docker volume. Do not
`docker compose up` bind mounts until the volume has been copied onto the host
(host folder can be days behind). Do not delete `panel-{slug}_storage` until
the host copy is verified. Do not treat matching OneDrive vs host counts as
proof the live app folder is complete.

**Tests.** `backend/tests/test_laravel_template.py`.

---

## 2026-09-15 — App files backup dumped whole storage/app into OneDrive

**Symptom.** An app-files job saved to a chosen OneDrive folder, but the remote
grew a nested `storage/app/public` and `storage/app/private` tree. There was no
way to back up only `storage/app/public/attachments` (files in that folder, not
the folder name itself).

**Cause.** Targets only listed coarse presets (`storage/app`, `public`,
`private`). New jobs default-checked all of them, so “App files” meant the whole
storage tree. rclone `dstFs` was `destination + "/" + rel`, which wrapped the
source path under the selected OneDrive folder.

**Fix.**

- Discover immediate children of those presets (attachments, thumbnails, …).
  Skip cache/framework/tmp names. Browse or type any nested path.
- Do not pre-check `storage/app`. The operator must pick the folder.
- Sync **contents** of each chosen folder into `destination` (`dstFs` is the
  selected remote folder). Do not append `storage/app/...` onto the dest.

**Do not.** Set `dstFs` to `dest + "/" + rel` again. Do not default-select
`storage/app` / public / private. Do not create the source folder name on the
remote when the job is “files in this folder”. Existing jobs that still have
`paths: ["storage/app"]` will now land `public/` and `private/` at dest root
(rclone sync also removes extra files already in that dest). Edit the job to
`storage/app/public/attachments` (and a dest meant only for those files).

**Tests.** `backend/tests/test_backup_files.py`.

---

## 2026-09-15 — rclone OneDrive: unable to get drive_id and drive_type

**Symptom.** Adding OneDrive in the panel (Storage wizard) signed in with Microsoft
successfully, then listing files failed: `unable to get drive_id and drive_type -
if you are upgrading from older versions of rclone, please run rclone config and
re-configure this backend`. The remote existed. Host `rclone config` for a
separate `onedrive_backup` remote worked.

**Cause.** rclone 1.75 will not open OneDrive unless `drive_id` and `drive_type`
are in the config. Those are filled only by the post-OAuth drive picker
(`config_type` → Graph `/me/drive(s)` → confirm). The wizard always passes
`all: true` and auto-answers advanced options, so rclone saved `type` + OAuth
`token` and marked the remote done. The panel uses Docker volume
`/config/rclone/rclone.conf`, not `~/.config/rclone/rclone.conf`. Graph itself
was fine; the token could list the business drive.

**Fix.**

- Do not auto-fill blank `drive_id` / `drive_type`. Default `config_type` to
  `onedrive` (SharePoint stays under advanced). Confirm the chosen drive.
- When the wizard finishes (and on list/about of that error), look up
  `/me/drive` and `config/update` the missing keys. Refresh the token on 401.
- Wrote `drive_id` / `drive_type=business` onto the live `onedrive` remote.

**Do not.** Treat this error as a Microsoft login failure or “upgrade rclone
config” on the host. Do not auto-answer `drive_id`/`drive_type` with empty
advanced defaults. Do not point the panel at `~/.config/rclone/`. Do not pick
SharePoint `PersonalCacheLibrary` as the default drive (`/me/drive` is the
user’s OneDrive).

**Tests.** `backend/tests/test_rclone_onedrive.py`.

---

## 2026-09-15 — Manual backup Run in a notification group sent no mail

**Symptom.** Clicking Run on a backup (Zuc Uz files) succeeded, but no email
arrived. Mail **Test** already worked. The job is in notification group
“Urudžbeni zapisnik” with two other backups.

**Cause.** Grouped jobs disable per-job mail. `record_group_run` only sends when
every job in the group has finished (or after the timeout). A manual Run of one
job was recorded into the pending batch and then went silent — the history log
did not even say it was waiting. Cron for the three jobs is `16:30` UTC, so the
other two were not going to run for hours.

**Fix.**

- A Run click (`manual=True`) still joins the group batch, but also sends a
  per-job status email using the group’s mail account and recipients.
- While waiting, the backup log says which jobs are outstanding.
- Scheduled group runs are unchanged (one combined email).

**Do not.** Flush the whole group on a manual Run (that would send a partial
report and start a new batch, breaking the daily combined mail). Do not treat
“no mail after Run” as an SMTP password failure when the job is in a group.

**Tests.** `backend/tests/test_notify_groups.py`.

---

## 2026-09-15 — Mail account Test button always failed

**Symptom.** After editing a mail account (or clicking **Test** on Mail), the
saved-account test returned 400 “No recipient address is set.” SMTP itself could
still work: the wizard test (`POST /api/mail/test`) sent fine to the From
address, and alerts that already had Recipients would still mail. It looked like
the new password was broken.

**Cause.** When Recipients replaced per-account `to_email` (`6f8f325`),
`send_message_sync` stopped falling back to `from_email`. `send_test_for_body`
(wizard) was updated to pass `to_email=account["from_email"]`. `send_test`
(Mail page **Test** on a saved account) was not, so it always raised before SMTP.

**Fix.** `send_test` sends the test to the account’s From address, same as the
wizard.

**Do not.** Restore a silent `from_email` fallback in `send_message_sync`. Real
alerts must keep using Recipients. Do not treat a failed Mail **Test** as proof
the SMTP password is wrong.

**Tests.** `backend/tests/test_mail.py`.

---

## 2026-09-14 — Log-scan mail and Postgres healthcheck noise

**Symptom.** After the queue-recycle mail fix, this VPS still mailed “Log error” for
`demo-app_postgres` (`FATAL: database "app_user" does not exist` every 5s),
`vps_dashboard` (SMTP `ValueError` / `Traceback`), `vps_dashboard_edge` (old Caddy
`connection refused`), and `nginx-nginx-1` (panel rebuild 502s plus a bot hit on
`GET /.env-traceback`). Apps were healthy. Queue “Restarted” mail had already stopped.

**Cause.**

- Postgres healthcheck was `pg_isready -U $USER` with no `-d $DB`. `pg_isready`
  connects to a database named after the user. When `DB_USERNAME` (`app_user`)
  differs from `DB_DATABASE` (`app_db`), every 5s probe logs FATAL while the
  container stays healthy.
- Log scan hashed the last 100 lines. Any new line changed the hash, then the
  last matching errors in that window were mailed — including months-old ones.
- The panel scanned `vps_dashboard` logs. Failed alert sends wrote `Traceback` /
  `ValueError`, which the next poll mailed again.
- `traceback` in a URL (`/.env-traceback`, HTTP 301) matched the severity regex.
- Exit 137 (SIGKILL from compose recreate / stop-timeout) was labeled OOM even
  when `OOMKilled` was false.

**Fix.**

- Template + live `docker-compose.panel.yml`: `pg_isready -U $USER -d $DB`. Recreated
  the noisy Postgres containers so the probe uses `app_db`.
- `scan_log_text` checkpoints the last Docker log timestamp. Only newer error lines
  mail. Leftover sha256 checkpoints bootstrap silently.
- Do not log-scan `vps_dashboard`.
- `log_match.py` strips HTTP request targets / JSON `uri` before severity keywords.
- OOM only when Docker sets `OOMKilled`. Exit 137 without that is a planned kill
  (same class as 143) in `container_status.py` and `CLEAN_EXIT_CODES`.

**Do not.** Ignore all Postgres `FATAL` (real DB failures must still mail). Do not
scan the dashboard’s own logs “to monitor the panel”. Do not treat exit 137 as OOM
without `OOMKilled`. Do not go back to hashing the whole log tail.

**Tests.** `backend/tests/test_log_matches.py`, `backend/tests/test_container_alerts.py`,
`backend/tests/test_laravel_template.py`.

**Still noisy (accepted).** nginx 502s while the panel itself is being rebuilt are
real for that window. Real app 5xx and new Caddy `http.log.error` still mail.

---

## 2026-09-14 — Template switch left backups on the old DB hostname

**Symptom.** Panel job “Demo App PostgreSQL” failed:
`pg_dump: could not translate host name "legacy-postgres-2" to address`.
File backups (attachments/thumbnails) succeeded. Last good SQL dump was just before
the app was switched onto the panel template. Same class of leftover: another app’s
old compose `container_name`.

**Cause.** Adopt creates `{slug}-db` → `{slug}_postgres` (live container `demo-app_postgres`)
but leaves the discovered connection `{slug}-postgres` → old `container_name`
(`legacy-postgres-2`). Scheduled jobs kept `connection_id: demo-app-postgres`. Discovery
skips `docker-compose.panel.yml`, so it kept reading the repo compose. `.env`
`DB_HOST=postgres` is treated as generic, so resolve fell back to the dead stored host.
`connect_dashboard_to_peer` no-ops if that container is gone. DNS then fails inside
`vps_dashboard`.

**Fix.**

- `laravel_svc.upsert_database_config` drops `{slug}-postgres` / `{slug}-mysql` (and
  other SQL connections whose `env_file` lives in the app folder) and writes `{slug}-db`.
- `backup_svc.repoint_connection_ids` rewrites jobs to `{slug}-db`. Runs on adopt and
  on panel start (`repoint_managed_database_targets`).
- `discover_databases` skips `managed` apps so old compose hostnames are not re-added.
- `db_svc._resolve_sql_server` falls back to `{slug}_postgres` / `{slug}_mysql` only
  when the stored host container is not running.

**Do not.** Recreate the old discovered SQL container. Do not dump from the dashboard
using the generic hostname `postgres` (several networks share that alias). Do not
leave discovered `{slug}-postgres` next to `{slug}-db` after a template switch.

**Tests.** `backend/tests/test_db_repoint.py`.

**Still true.** File backup jobs do not use a DB hostname. SQLite template addons do
not get a `{slug}-db` SQL connection, so a Postgres/MySQL dump job is not converted
to SQLite. Same-engine switches (Postgres → panel Postgres) are the supported remap.

---

## 2026-09-14 — Queue worker hourly mail alerts (false positive)

**Symptom.** Mail every ~1 hour for `demo-app_queue`: “Restarted”, then “Restart loop”
(“This app keeps crashing…”). Docker page looked healthy most of the time. Health `—`.

**Cause.** Not a crash. Panel queue command uses Laravel `--max-time=3600`, so the worker
exits cleanly every hour. `restart: unless-stopped` starts it again. The alerter treated
any `restarting` snapshot as a loop, and any `RestartCount` bump as “Restarted”.

**Fix.** In `backend/app/services/container_alerts_svc.py`:

- Do **not** mail `restart_loop` on a single brief `restarting` poll. Require 2 consecutive
  polls still restarting (~60s) **or** 3 restarts inside 10 minutes.
- Do **not** mail `restart_count` for expected recyclers (`*_queue`, compose service `queue`,
  `queue:work` / `horizon` / `--max-time` / `--max-jobs`) on a single clean recycle.
- Also skip `restart_count` when this cycle’s exit was `0` or `143` (SIGTERM / docker restart).
  That also stops “Restart loop” mail from a one-off `docker restart`.

In `backend/app/services/container_status.py`: a `restarting` container with exit `0`/`143`
is **not** a crash-loop issue on the Docker page.

**Do not.** Remove `--max-time=3600` from `laravel_svc.py` / compose to “stop the restarts”.
That recycle is intentional (worker memory). Fix alerting, not the worker.

**Tests.** `backend/tests/test_container_alerts.py`.

**Still noisy (accepted).** Clicking Restart on a non-queue container, if the poll misses the
`restarting` window, can still send “Restarted”. Real crash-restarts of `*_app` still mail.

---

## 2026-09-08 — Log-scan false positives on hashed frontend assets

**Symptom.** “Scan logs for errors” matched nginx/Caddy lines that only mentioned Vite files
such as `input-error-DEkipNxA.js` (HTTP 200).

**Cause.** Substring match on `error` inside filenames and `/build/assets/…` URLs.

**Fix.** `backend/app/services/log_match.py` — ignore asset URLs/filenames; only treat HTTP
5xx and real severity tokens as errors.

**Do not.** Go back to a raw `if "error" in line.lower()` matcher.

**Tests.** `backend/tests/test_log_matches.py`.

---

## 2026-09-16 — File-backup email sync stats

**Symptom.** File backup emails only showed status, times, destination, and rclone log
snippets — no summary of how many files were uploaded, unchanged, or removed this run.

**Cause.** `sync/sync` ran without an RC `_group`, and history entries did not store rclone
transfer counters.

**Fix.**

- Pass a unique `_group` per sync (`panel-backup-{run_id}` or `-{index}` for multiple
  folders), then read **`core/stats` with the same `group`** immediately after sync.
- Store normalized counters on the history entry as `sync_stats`; show them in backup and
  group notification emails for `app_files` and `path` jobs.

**Do not.** Use unscoped global `core/stats` (accumulates since `rclone rcd` started). Do not
call `operations/size` on the remote by default for “total files” — extra full-tree API load
and misleading vs live app folders (see 2026-09-15 adopt note on count comparisons).

**Tests.** `backend/tests/test_backup_files.py`, `backend/tests/test_mail.py`.
