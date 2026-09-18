# VPSops

Self-hosted control panel for **one Docker VPS**. Install it on each server you want to manage. It is not a fleet manager: each clone talks only to the Docker socket on that machine.

From the browser you can:

- see host CPU, memory, disk, and load, and control containers
- publish websites (nginx + Let’s Encrypt, started from the panel if you do not have them yet)
- deploy Laravel apps from GitHub or an existing folder, or pick up compose apps that are already running
- browse MySQL / Postgres / SQLite
- back up to cloud storage (rclone) and get email alerts for containers, certificates, and backup jobs

The panel itself stays on **port 9090**. Public sites use **80 and 443**.

**New server?** Follow [docs/NEW_VPS.md](docs/NEW_VPS.md) (agents: [AGENTS.md](AGENTS.md)). Existing Laravel apps: [docs/ADOPT.md](docs/ADOPT.md). Dash bugs already fixed: [docs/AUDIT.md](docs/AUDIT.md).

---

## What you need

- A Linux VPS on **x86_64 or aarch64** (Oracle Ampere, Hetzner CAX, 64-bit Raspberry Pi). 32-bit ARM is not supported.
- [Docker](https://docs.docker.com/engine/install/) with Compose v2 (`docker compose version`)
- The user that runs Compose must be able to use Docker (usually `docker` group, or root)
- Outbound HTTPS (images, GitHub, Let’s Encrypt)

This panel mounts **`/var/run/docker.sock`**. Anyone with the Docker privilege can start, stop, and delete containers on that VPS. Treat owner (and Docker) access like root.

---

## New VPS

On another server, clone this repo and follow **[docs/NEW_VPS.md](docs/NEW_VPS.md)** step by step. That document is written so a human or an agent can recreate nginx+certbot and Laravel deploys without copying `apps.json` or volumes from here.

---

## Install

SSH into the VPS, then:

```bash
git clone https://github.com/mgtr95/vps-ops-panel.git
cd vps-ops-panel

cp .env.example .env
```

You do **not** set a login in `.env`. The first time you open the panel, the browser asks for the owner username, email, and password.

Optional: if this VPS already has apps, point the panel at that folder (compose files live here):

```env
HOST_APPS_DIR=/home/YOUR_USER/apps
```

Leave `HOST_APPS_DIR` unset only if you have no apps yet. The panel will create managed app folders under `./.keep/apps` inside the clone.

Optional, if git pull of private repos should use your keys:

```env
HOST_SSH_DIR=/home/YOUR_USER/.ssh
HOST_GITCONFIG=/home/YOUR_USER/.gitconfig
```

You do **not** need to copy `config/apps.json`, `config/databases.json`, or `docker-compose.override.yml`. Those are created automatically when needed.

Start it:

```bash
docker compose up -d --build
```

Open:

```text
http://YOUR_VPS_IP:9090
```

Your VPS firewall must allow **9090**. For public websites later, also allow **80** and **443**.

---

## First login

1. Open the panel. On a fresh install you get **Create the owner account**.
2. Enter a username, email, and password (at least 12 characters).
3. If `REQUIRE_2FA=true` (the default), scan the QR code with an authenticator app (Aegis, Google Authenticator, 1Password, …) and enter the 6-digit code.

Later, change password, email, and 2FA from **Security**. Extra dashboard users are also added there. Each user gets **modules** (Docker, Apps, Websites, …) and nested **actions** (for example Docker: Start / Stop / Logs; Databases: Edit rows / Run SQL). Non-owners with **Manage users** can only grant actions they themselves have. **Settings** stays owner-only.

If you get locked out, use **Forgot password** (needs Mail plus an email on the account), wait 15 minutes (default lockout), or see [Troubleshooting](#troubleshooting).

---

## After login — recommended order

### 1. Websites (do this before publishing a domain)

Open **Websites**.

- **If nginx and certbot are already running**, the page says hosting is on. Add an email if it asks, so new certificates can be issued.
- **If they are not running**, follow the three steps: ports 80 and 443 must be free, enter an email for Let’s Encrypt, click **Start website hosting**. The panel creates a Docker network, starts nginx + certbot, and keeps certificates renewed.

Do not put another reverse proxy on 80/443 at the same time (including the optional `docker-compose.https.yml` for the panel). The panel stays on 9090; nginx owns 80/443 for your sites. After hosting is on, the owner can open **Settings** and apply a domain for the panel itself (certificate + HTTPS on that hostname).

### 2. Apps that are already on the server

Open **Apps**. Compose projects under `HOST_APPS_DIR` show up on their own (git pull, rebuild, restart). The panel skips itself and nginx.

**Docker** lists every container, including ones that are not compose apps.

**Databases** picks up MySQL from an app `.env` (passwords stay in that `.env`). SQLite files on disk are listed when the panel can see them; databases that live only inside a Docker volume need a line in `config/databases.json` or an extra volume in `docker-compose.override.yml`.

### 3. Deploy a new Laravel app from GitHub

Still on **Apps**:

1. **Connect GitHub** — the panel walks you through creating a GitHub App (one-time). Grant it access to the repositories you want.
2. **New Laravel app** — pick the repo, database (MySQL / Postgres / SQLite / none), extras (Redis, queue, scheduler, Mailpit, extra apt packages, extra PHP extensions). The repo does **not** need Docker files; the panel generates them.
3. Optional domain — at your registrar, create an **A record** for `@` (and `www` if you want) pointing at this VPS IP. The panel shows the IP.
4. **Deploy**. When DNS is ready, **Go live** on the app page writes nginx config, requests a certificate, and reloads websites.

A Laravel folder already on disk: **Deploy folder** on Apps, or open a discovered app and **Switch to panel template**. You can copy existing database data into the new stack; a local dump is written under `.panel-backups/` first (see [docs/ADOPT.md](docs/ADOPT.md)). Production never uses the repo’s own compose/Dockerfile.

The app page **Deployments** section shows the CI/CD pipeline: checkout → backup → PHP → frontend → image → go live → copy data → migrate → health. Later pushes to the chosen branch can auto-deploy (GitHub webhook) if you leave that option on.

Non-Laravel compose apps stay on the **On this server** list (pull / rebuild / restart). Laravel apps should be switched to the panel template instead of rebuilding their old compose file.

---

## Using the rest of the panel

| Page | What it is for |
| --- | --- |
| **Overview** | Host Performance (CPU breakdown, memory, load and uptime); unhealthy containers; certificates; recent backups |
| **Docker** | Start / stop / logs / inspect containers, images, compose files; **Why** on problem containers; **Container alerts** |
| **Apps** | GitHub Laravel pipeline (**Deployments**), discovered compose apps, **Deploy folder** / **Switch to panel template** |
| **Websites** | nginx/certbot status, certificate list, renew, reload; **Certificate alerts** |
| **Databases** | Browse and edit tables, run SQL, import/export |
| **Rclone** | Connect Google Drive, S3, etc. (same idea as `rclone config`) |
| **Mail** | SMTP **accounts** (senders) and reusable **Recipients** (backup alerts, password-reset emails) |
| **Backups** | Scheduled copies of databases or app folders; custom email copy; **Notification groups** |
| **Settings** | Panel domain (HTTPS via nginx + Let’s Encrypt). Owner only. |
| **Security** | Password, email, enable/disable 2FA, extra users with module and action privileges |

Backups: add **Rclone**, a **Mail** account, and **Recipients** first if you want off-server copies and email. Then create a job on **Backups** (daily/weekly, timezone, what to copy) and optionally a **Notification group**.

---

## Email notifications

Mail **accounts** send mail. **Recipients** are the people who receive it. Add both on **Mail** before enabling alerts. You can also type extra email addresses on a job or alert.

1. **Mail → Add mail account** — SMTP sender (Gmail, Outlook, Yahoo, iCloud, or custom). Send a test, then save.
2. **Mail → Recipients → Add recipient** — name and email, reused across backups, Docker alerts, and certificate alerts. Optional **Include backup details and logs**: off means that person gets only the subject, title, and custom message.

Then enable the alerts you want:

- **Backups** — on a job, **Email me when this backup runs**. Choose the mail account, recipients, extra emails, and when (every run / only if it fails / only if it succeeds). Optionally set a custom subject, title, and message for success and for failure (`{name}` is replaced with the backup name).
- **Backups → Notification groups** — one email for several jobs. Set a timeout so the group still sends if some jobs have not finished. Per-job email is disabled while a job is in a group.
- **Docker → Container alerts → Configure** — crash or fatal stop, restart loop, unhealthy health check, out of memory, container restarted; optional **Scan logs for errors**. Alerts are limited to once every few minutes per container.
- **Websites → Certificate alerts → Configure** — expiring soon (30, 14, 7, 1 days), expired, renewal succeeded, renewal failed, disk vs served certificate mismatch.

Each alert needs a mail account and at least one recipient or extra address.

---

## How you open the panel

Default: **no domain**, browser to `http://VPS_IP:9090`. Public sites still get HTTPS via **Websites**.

To use a hostname for the panel itself, either:

1. **Settings** (recommended when Websites/nginx already owns 80/443) — paste `panel.example.com`, point an A record at this VPS, click **Apply**. The panel issues a Let’s Encrypt certificate, reloads nginx, and opens `https://panel.example.com`. Port 9090 stays available as a fallback. Applied Settings domain is stored in the data volume and overrides `PANEL_DOMAIN` until you remove it.
2. **`.env`** — set `PANEL_DOMAIN=panel.example.com`. With `PANEL_AUTO_HTTPS=false`, terminate TLS on your own proxy and point it at `http://127.0.0.1:9090`. With `PANEL_AUTO_HTTPS=true`, also start `docker-compose.https.yml` so Caddy can bind 80+443.

`docker-compose.https.yml` publishes 80+443 on the panel’s Caddy. Use that **only** if this VPS has no nginx (and you will not use **Start website hosting**). For a normal app server, use **Settings** (or your own nginx vhost) instead. Do not run Caddy auto-HTTPS and app nginx on 80/443 together.

---

## Updating

```bash
cd vps-ops-panel
git pull
docker compose up -d --build
```

Data lives in Docker volumes (`dashboard_data`, rclone config). App files live on the host under `HOST_APPS_DIR`.

---

## Security notes

- The first account is created in the browser on first open. Keep **2FA** on (`REQUIRE_2FA=true`). When that is set, nobody can disable 2FA from Security. If it is `false`, each user can enable or disable 2FA on their own account.
- A signing secret is created automatically on first start. If you set `SESSION_SECRET` in `.env` yourself, do not change it later unless you accept that stored mail passwords and GitHub App secrets must be saved again.
- Add an email on **Security** and a mailbox on **Mail** if you want “Forgot password” links.
- Extra users are created on **Security**. Turn on only the modules and actions they need. Docker, apps, and databases are as powerful as root on this VPS. The first account is the owner and cannot be deleted or demoted. Non-owners with **Manage users** can only grant actions they themselves have.
- Prefer not exposing 9090 on the public internet without a firewall (allow your IP) or HTTPS in front of it.
- Login is rate-limited (5 failures → 15 minute lockout per IP, configurable in `.env`).
- Do not commit `.env`, `config/apps.json`, or `config/databases.json`.

---

## Configuration reference

| Item | Role | In git? |
| --- | --- | --- |
| `.env` | Optional mounts, timezone, `SESSION_SECRET` | No |
| `config/apps.json` | App list (auto-filled) | No |
| `config/databases.json` | DB connections (auto-filled) | No |
| `docker-compose.override.yml` | Extra host mounts only if you need them | No |
| `docker-compose.https.yml` | Optional panel HTTPS on 80/443 | Yes |

Useful `.env` keys (see `.env.example` for the full list):

| Variable | Meaning |
| --- | --- |
| `HOST_APPS_DIR` | Host folder mounted as `/apps` (discovery + deploys) |
| `COMPOSE_SCAN_PATHS` | Extra paths *inside the container* to scan (default `/apps`) |
| `PANEL_PORT` | Host port for the panel (default `9090`) |
| `PANEL_DOMAIN` | Optional panel hostname in `.env` (Settings can set this at runtime instead) |
| `TZ` | Clock for backup schedules (default `UTC`) |
| `HOST_LETSENCRYPT_DIR` | Existing Let’s Encrypt tree if you already use `/etc/letsencrypt` |

Host stats on **Overview** (CPU, memory, load, uptime) come from read-only `/proc` mounts in `docker-compose.yml` (`/host/proc/...`). They are the VPS, not the panel container. You do not set these in `.env`. Disk usage is the Docker data filesystem.

`docker-compose.override.example.yml` is only for extra volumes (for example mounting a SQLite file). Website networking is handled inside the panel.

---

## Troubleshooting

**Cannot log in / forgot the owner password**  
Use **Forgot password** (if Mail and an account email are set), or remove the `dashboard_data` volume and create the owner again in the browser (this wipes panel login, 2FA, mail, GitHub App, and backup job definitions — not your apps). Editing `.env` does not change stored passwords.

**Forgot password does nothing**  
The account needs an email on **Security**, and **Mail** needs at least one mailbox. The sign-in page only shows “Forgot password?” when a mailbox is saved.

**No notification emails**  
Save a mail account on **Mail**, add recipients (or extra addresses on the job/alert), and turn the alert on. For backups, either enable email on the job or put it in a **Notification group**.

**Cannot disable 2FA**  
`REQUIRE_2FA=true` blocks that. Set it to `false` in `.env` and recreate the panel container if you want per-user 2FA. Someone with the **Manage users** privilege can also clear another user’s 2FA (not the owner’s) so they can enroll again.

**Websites says ports 80/443 are busy**  
Something else is bound there. Open **Docker**, stop that container, then return to Websites. Do not run `docker-compose.https.yml` together with app nginx.

**Overview CPU / memory looks like the container, not the VPS**  
Those numbers need the `/proc` mounts in `docker-compose.yml`. After `git pull`, run `docker compose up -d --build` so the dashboard container is recreated with them.

**Apps list is empty but containers exist**  
Set `HOST_APPS_DIR` to the directory that contains those projects’ `docker-compose.yml`, then `docker compose up -d` again (so the mount is correct) and refresh **Apps**.

**Git pull fails for a private repo**  
Mount SSH keys with `HOST_SSH_DIR`, or deploy through **Connect GitHub** so the GitHub App token is used.

**Certificate / Go live fails**  
DNS A record must point at this VPS. Wait for DNS, then **Check DNS** / **Go live** again. Websites must already be on, with an email saved.

**Settings → Apply domain fails**  
Same as Go live: Websites must be on, DNS A record must point at this VPS, then **Check DNS** / **Apply** again. Do not enable `docker-compose.https.yml` while nginx is using 80/443.

**`docker compose` build fails on 32-bit ARM**  
The image supports x86_64 and aarch64 only. Use a 64-bit OS (for example Raspberry Pi OS 64-bit).

---

## License

MIT
