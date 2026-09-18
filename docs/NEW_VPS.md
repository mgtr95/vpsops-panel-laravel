# New VPS — exact bootstrap

**Paste this to an agent on a fresh server:**

> Follow `docs/NEW_VPS.md` in this repo exactly. Install VPSops on this machine, start nginx+certbot from the panel (not Caddy on 80/443), and deploy Laravel+React apps only via the panel template. Do not use a repo’s own Dockerfile/compose for production. Do not copy `config/apps.json`, `.env`, or volumes from another VPS.

This file is the source of truth. Do not skip steps or add Traefik/Caddy/FrankenPHP TLS.

---

## 0. What you get

- Panel on **port 9090**
- **nginx + certbot** on **80/443** (started from the panel, not by hand)
- Shared Docker network **`vps_proxy`**
- Laravel+React deploys: panel generates FrankenPHP on **`:80`**; nginx `proxy_pass`es to `{slug}_app:80`

A Laravel repo **does not need Docker files**.

---

## 1. Docker Engine + Compose v2

On Ubuntu/Debian as a user with sudo:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`), then:

```bash
docker compose version
```

You need Compose **v2** (`docker compose`, not `docker-compose` v1).

Architecture: **x86_64 or aarch64** only.

---

## 2. Clone the panel (do not copy another VPS’s config)

Replace the URL with your fork if needed:

```bash
cd ~
git clone https://github.com/mgtr95/vps-ops-panel.git
cd vps-ops-panel
cp .env.example .env
```

**Do not** copy `config/apps.json`, `config/databases.json`, `.env`, or Docker volumes from another server.

---

## 3. `.env` on this machine

Edit `.env`:

```env
HOST_APPS_DIR=/home/YOUR_USER/apps
TZ=Europe/Zagreb
PANEL_PORT=9090
PANEL_DOMAIN=
PANEL_AUTO_HTTPS=
```

```bash
mkdir -p /home/YOUR_USER/apps
```

- Leave `PANEL_AUTO_HTTPS` empty/false.
- **Do not** run `docker compose -f docker-compose.https.yml …` — that binds Caddy on 80/443 and fights nginx.

Optional private git over SSH:

```env
HOST_SSH_DIR=/home/YOUR_USER/.ssh
HOST_GITCONFIG=/home/YOUR_USER/.gitconfig
```

---

## 4. Start the panel

```bash
docker compose up -d --build
```

Open:

```text
http://YOUR_VPS_IP:9090
```

Firewall: allow **9090** (prefer your IP only), and later **80** and **443**.

---

## 5. First login

1. Create the owner account (password ≥ 12 characters).
2. If `REQUIRE_2FA=true`, scan the QR code and enter the 6-digit code.

---

## 6. Website hosting (nginx + certbot)

1. Open **Websites**.
2. Ports **80 and 443 must be free**.
3. Enter a Let’s Encrypt email.
4. Click **Start website hosting**.

This creates `vps_nginx`, `vps_certbot`, and the `vps_proxy` network.

If this VPS already has nginx+certbot you want to keep, the panel can detect them. Do not start a second pair on 80/443. See [Existing stack](#existing-stack-nginx-or-apps-already-here).

Optional: **Settings** → panel hostname (A record → this IP) so you can use `https://panel.example.com` instead of `:9090`.

---

## 7. Connect GitHub

**Apps** → **Connect GitHub**. Create the GitHub App when asked and grant the Laravel repos you will deploy.

---

## 8. Deploy a Laravel + React app

Repo needs `artisan` + `composer.json` (and usually Vite). **No Dockerfile required.**

1. **New Laravel app**
2. Pick repo + branch
3. Database: MySQL / Postgres / SQLite / none
4. Optional: Redis, queue, scheduler, Mailpit (staging only), extra apt/PHP packages
5. Domain optional (A record at this VPS IP)
6. **Deploy**
7. When DNS is ready: **Go live** (certificate + nginx `proxy_pass` to `{slug}_app:80`)

Pushes to the chosen branch auto-deploy unless you turn that off.

**Folder already on disk:** Apps → **Deploy folder** → folder name under `HOST_APPS_DIR`. Same template.

**App discovered with old compose:** open it → **Switch to panel template**. Do not use Pull + Rebuild on Laravel apps — that runs the repo’s Docker.

---

## 9. Updates

```bash
cd ~/vps-ops-panel
git pull
docker compose up -d --build
```

Panel data stays in Docker volumes. Apps stay under `HOST_APPS_DIR`.

---

## Do not copy between VPS

| Item | Why |
|---|---|
| `config/apps.json` | App IDs and paths are per machine |
| `config/databases.json` | Secrets/hosts differ |
| `.env` | `HOST_APPS_DIR` and secrets differ |
| `dashboard_data` volume | Login, GitHub App, 2FA |
| nginx FastCGI vhosts / bind-mounts | Legacy; new apps use `proxy_pass` |

Re-add apps from GitHub (or a folder) on the new VPS. Let’s Encrypt issues **new** certs here.

---

## Existing stack (nginx or apps already here)

1. Set `HOST_APPS_DIR` to the folder that contains those projects.
2. Do not bind 80/443 twice. If nginx+certbot already run, open **Websites** and let the panel detect them; skip **Start website hosting** if it already says hosting is on.
3. Laravel apps that still use PHP-FPM FastCGI: open each app → **Switch to panel template**. The panel writes a local DB dump first and can copy data into the new database. See [ADOPT.md](ADOPT.md).
4. After every public Laravel app is on the template, nginx can drop host bind-mounts (this repo’s sibling `nginx/docker-compose.proxy-only.yml` on servers that use a separate nginx compose).
