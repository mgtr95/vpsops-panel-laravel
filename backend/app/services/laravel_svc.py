"""Detect Laravel apps and generate the panel-managed runtime (compose, env, Dockerfile)."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.services import github_svc
from app.utils import parse_dotenv, read_json, slugify, write_dotenv, write_json

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "laravel"

PHP_RE = re.compile(r"(\d+)\.(\d+)")
SAFE_PKG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9.+-]*$")
PANEL_COMPOSE = "docker-compose.panel.yml"
# Laravel 11+ exposes GET /up. Laravel 10 and older 404 that path, so Docker
# would mark a working app unhealthy. Probe /up, then /.
APP_HTTP_HEALTHCHECK = (
    "curl -fsS -o /dev/null http://127.0.0.1/up 2>/dev/null || "
    "curl -fsS -o /dev/null http://127.0.0.1/"
)
# pg_isready defaults to a database named after -U. When DB_USERNAME !=
# DB_DATABASE that logs FATAL every health interval while still reporting
# healthy. See docs/AUDIT.md.
POSTGRES_HEALTHCHECK = (
    "pg_isready -U ${DB_USERNAME:-laravel} -d ${DB_DATABASE:-laravel}"
)


def panel_project_name(slug: str) -> str:
    return f"panel-{slug}"


def panel_compose_cmd(slug: str, *args: str) -> list[str]:
    return ["docker", "compose", "-p", panel_project_name(slug), "-f", PANEL_COMPOSE, *args]


DEFAULT_PHP_EXTENSIONS = (
    "pcntl",
    "intl",
    "zip",
    "gd",
    "exif",
    "opcache",
    "pdo_mysql",
    "pdo_pgsql",
    "pdo_sqlite",
    "redis",
)

# Bind-mounted into FrankenPHP conf.d so per-app limits apply without rebuild.
PHP_INI_MOUNT = "./.panel/php/zz-panel.ini:/usr/local/etc/php/conf.d/zz-panel.ini:ro"

DEFAULT_PHP_LIMITS = {
    "upload_max_filesize_mb": 20,
    "post_max_size_mb": 30,
    "memory_limit_mb": 512,
    "max_execution_time": 120,
    "max_input_time": 120,
    "client_max_body_size_mb": 32,
}


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def normalize_php(raw: dict | None) -> dict:
    """Per-app PHP / nginx upload and runtime limits (MB and seconds)."""
    data = raw or {}
    upload = _clamp_int(
        data.get("upload_max_filesize_mb"),
        DEFAULT_PHP_LIMITS["upload_max_filesize_mb"],
        1,
        512,
    )
    post_default = max(DEFAULT_PHP_LIMITS["post_max_size_mb"], upload)
    post = _clamp_int(data.get("post_max_size_mb"), post_default, 1, 1024)
    if post < upload:
        post = upload
    memory = _clamp_int(
        data.get("memory_limit_mb"),
        DEFAULT_PHP_LIMITS["memory_limit_mb"],
        64,
        4096,
    )
    max_exec = _clamp_int(
        data.get("max_execution_time"),
        DEFAULT_PHP_LIMITS["max_execution_time"],
        0,
        3600,
    )
    max_input = _clamp_int(
        data.get("max_input_time"),
        DEFAULT_PHP_LIMITS["max_input_time"],
        0,
        3600,
    )
    body_default = max(post, DEFAULT_PHP_LIMITS["client_max_body_size_mb"])
    body = _clamp_int(data.get("client_max_body_size_mb"), body_default, 1, 1024)
    if body < post:
        body = post
    return {
        "upload_max_filesize_mb": upload,
        "post_max_size_mb": post,
        "memory_limit_mb": memory,
        "max_execution_time": max_exec,
        "max_input_time": max_input,
        "client_max_body_size_mb": body,
    }


def php_ini_text(php: dict | None = None) -> str:
    limits = normalize_php(php)
    return (
        f"upload_max_filesize = {limits['upload_max_filesize_mb']}M\n"
        f"post_max_size = {limits['post_max_size_mb']}M\n"
        f"memory_limit = {limits['memory_limit_mb']}M\n"
        f"max_execution_time = {limits['max_execution_time']}\n"
        f"max_input_time = {limits['max_input_time']}\n"
    )


def write_php_ini(path: Path, php: dict | None = None) -> Path:
    limits = normalize_php(php)
    dest_dir = path / ".panel" / "php"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "zz-panel.ini"
    dest.write_text(php_ini_text(limits))
    return dest


def sync_php_runtime(path: Path, slug: str, addons: dict, php_limits: dict | None = None) -> dict:
    """Write zz-panel.ini and regenerate compose so the bind mount exists. Leaves .env alone."""
    limits = normalize_php(php_limits)
    write_php_ini(path, limits)
    network = proxy_network_name()
    compose = generate_compose(slug, addons, network)
    (path / "docker-compose.panel.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
    )
    return limits


async def compose_up_no_build(path: Path, slug: str) -> None:
    """Apply compose changes (including new bind mounts) without rebuilding the image."""
    import asyncio
    import os

    from app.services import deploy_svc

    cwd = str(path)
    cmd = panel_compose_cmd(slug, "up", "-d", "--no-build")
    env_file = deploy_svc.detect_compose_env_file(path, PANEL_COMPOSE)
    if env_file:
        cmd = [
            "docker",
            "compose",
            "--env-file",
            env_file,
            "-p",
            panel_project_name(slug),
            "-f",
            PANEL_COMPOSE,
            "up",
            "-d",
            "--no-build",
        ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        text = (out or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(text or "compose up failed")


def _constraint_php_floor(constraint: str) -> tuple[int, int] | None:
    """Lowest PHP version named in a Composer constraint (OR branches)."""
    floors: list[tuple[int, int]] = []
    for part in re.split(r"\s*\|\|?\s*", constraint or ""):
        match = PHP_RE.search(part)
        if match:
            floors.append((int(match.group(1)), int(match.group(2))))
    return min(floors) if floors else None


def php_version_from_composer(composer: dict, lock: dict | None = None) -> str:
    require = composer.get("require") or {}
    candidates: list[tuple[int, int]] = []
    floor = _constraint_php_floor(str(require.get("php") or ""))
    if floor:
        candidates.append(floor)
    laravel = str(require.get("laravel/framework") or "")
    laravel_match = PHP_RE.search(laravel)
    if laravel_match and int(laravel_match.group(1)) >= 12:
        # Laravel 12 lockfiles commonly need 8.3+ even when composer.json says ^8.2.
        candidates.append((8, 3))
    if laravel_match and int(laravel_match.group(1)) >= 13:
        # Laravel 13+ needs PHP 8.4 even when composer.json still says ^8.3.
        candidates.append((8, 4))
    # composer.json often says ^8.2 while lock installs Symfony 8 (>=8.4).
    for pkg in (lock or {}).get("packages") or []:
        pkg_floor = _constraint_php_floor(str((pkg.get("require") or {}).get("php") or ""))
        if pkg_floor:
            candidates.append(pkg_floor)
    if not candidates:
        return "8.3"
    major, minor = max(candidates)
    # FrankenPHP 8.2 + composer --ignore-platform-reqs installs packages that
    # then fail platform_check (>=8.3). Never ship 8.2 as the runtime.
    if major < 8 or (major == 8 and minor < 3):
        return "8.3"
    if major == 8 and minor > 4:
        return "8.4"
    return f"{major}.{minor}"


def is_laravel_composer(composer: dict) -> bool:
    require = composer.get("require") or {}
    return "laravel/framework" in require


def detect_from_files(
    composer_text: str | None,
    has_artisan: bool,
    lock_text: str | None = None,
) -> dict:
    if not composer_text or not has_artisan:
        return {
            "laravel": False,
            "reason": "This is not a Laravel app. This panel only deploys Laravel repositories (they have artisan and laravel/framework).",
        }
    try:
        composer = json.loads(composer_text)
    except json.JSONDecodeError:
        return {"laravel": False, "reason": "composer.json is not valid JSON."}
    if not is_laravel_composer(composer):
        return {
            "laravel": False,
            "reason": "composer.json does not require laravel/framework.",
        }
    lock: dict | None = None
    if lock_text:
        try:
            parsed = json.loads(lock_text)
            if isinstance(parsed, dict):
                lock = parsed
        except json.JSONDecodeError:
            lock = None
    return {
        "laravel": True,
        "php": php_version_from_composer(composer, lock),
        "laravel_version": str((composer.get("require") or {}).get("laravel/framework") or ""),
    }


async def preview_repo(owner: str, repo: str, branch: str) -> dict:
    artisan = await github_svc.get_file(owner, repo, "artisan", branch)
    composer_text = await github_svc.get_file(owner, repo, "composer.json", branch)
    lock_text = await github_svc.get_file(owner, repo, "composer.lock", branch)
    package = await github_svc.get_file(owner, repo, "package.json", branch)
    detected = detect_from_files(composer_text, artisan is not None, lock_text)
    detected["owner"] = owner
    detected["repo"] = repo
    detected["branch"] = branch
    detected["has_frontend"] = package is not None
    try:
        meta = await github_svc.get_repo(owner, repo)
        detected["default_branch"] = meta.get("default_branch") or branch
        detected["description"] = meta.get("description") or ""
        detected["private"] = meta.get("private")
    except Exception:
        detected["default_branch"] = branch
    try:
        detected["branches"] = await github_svc.list_branches(owner, repo)
    except Exception:
        detected["branches"] = [branch]
    return detected


def detect_local(path: Path) -> dict:
    artisan = path / "artisan"
    composer_path = path / "composer.json"
    if not artisan.exists() or not composer_path.exists():
        return detect_from_files(None, False)
    lock_path = path / "composer.lock"
    lock_text = lock_path.read_text() if lock_path.exists() else None
    detected = detect_from_files(composer_path.read_text(), True, lock_text)
    detected["has_frontend"] = (path / "package.json").exists()
    return detected


def proxy_network_name() -> str:
    from app.services import proxy_svc

    return proxy_svc.ensure_network()


def unique_slug(name: str, existing_ids: set[str]) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while slug in existing_ids:
        slug = f"{base}-{n}"
        n += 1
    return slug


def app_dir(slug: str) -> Path:
    return Path(get_settings().apps_root) / slug


def _safe_packages(names: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        item = str(raw).strip()
        if not item or not SAFE_PKG_RE.match(item) or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _split_packages(value: str | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return _safe_packages([str(v) for v in value])
    parts = [p.strip() for p in str(value).replace(",", " ").split()]
    return _safe_packages(parts)


def normalize_addons(raw: dict | None) -> dict:
    data = raw or {}
    database = str(data.get("database") or "none").strip().lower() or "none"
    if database not in {"mysql", "postgres", "sqlite", "none"}:
        database = "none"
    queue_on = bool(data.get("queue"))
    if queue_on and database in {"none", ""}:
        database = "sqlite"
    return {
        "database": database,
        "redis": bool(data.get("redis")),
        "queue": queue_on,
        "scheduler": bool(data.get("scheduler")),
        "mailpit": bool(data.get("mailpit")),
        "packages": _split_packages(data.get("packages")),
        "php_extensions": _split_packages(data.get("php_extensions")),
    }


def infer_addons(path: Path) -> dict:
    env_path = path / ".env"
    env = parse_dotenv(env_path) if env_path.exists() else {}
    conn = (env.get("DB_CONNECTION") or "").lower()
    if conn in {"mysql", "mariadb"}:
        database = "mysql"
    elif conn in {"pgsql", "postgres", "postgresql"}:
        database = "postgres"
    elif conn == "sqlite":
        database = "sqlite"
    else:
        database = "none"
    redis_on = (env.get("REDIS_HOST") or "").strip() not in {"", "127.0.0.1", "localhost"} or (
        env.get("CACHE_STORE") or env.get("CACHE_DRIVER") or ""
    ).lower() == "redis"
    queue_conn = (env.get("QUEUE_CONNECTION") or "sync").lower()
    queue_on = queue_conn not in {"", "sync", "null", "none"}
    return normalize_addons(
        {
            "database": database,
            "redis": redis_on,
            "queue": queue_on,
            "scheduler": False,
            "mailpit": False,
        }
    )


def restart_containers_for(slug: str, addons: dict) -> list[str]:
    names = [f"{slug}_app"]
    db = (addons.get("database") or "none").lower()
    if db == "mysql":
        names.append(f"{slug}_mysql")
    elif db == "postgres":
        names.append(f"{slug}_postgres")
    if addons.get("redis"):
        names.append(f"{slug}_redis")
    if addons.get("queue"):
        names.append(f"{slug}_queue")
    if addons.get("scheduler"):
        names.append(f"{slug}_scheduler")
    if addons.get("mailpit"):
        names.append(f"{slug}_mailpit")
    return names


def apply_panel_template(app: dict, addons: dict | None = None) -> dict:
    """Mark an app as panel-managed. Production Docker is generated, never the repo compose."""
    slug = app["id"]
    path = Path(app.get("path") or app_dir(slug))
    merged = normalize_addons(addons if addons is not None else app.get("addons") or infer_addons(path))
    app["kind"] = "laravel"
    app["managed"] = True
    app["needs_template"] = False
    app["compose_file"] = PANEL_COMPOSE
    app["rebuild_cmd"] = panel_compose_cmd(slug, "up", "-d", "--no-build")
    app["restart_containers"] = restart_containers_for(slug, merged)
    app["addons"] = merged
    app["php"] = normalize_php(app.get("php"))
    if (app.get("github") or {}).get("full_name"):
        app["source"] = "github"
    elif app.get("source") not in {"github"}:
        app["source"] = "local"
    return app


def dockerfile_text(php: str, addons: dict | None = None) -> str:
    extras = normalize_addons(addons)
    php_ext = list(DEFAULT_PHP_EXTENSIONS)
    for ext in extras.get("php_extensions") or []:
        if ext not in php_ext:
            php_ext.append(ext)
    apt_pkgs = ["git", "unzip", "sqlite3", "curl", *extras.get("packages", [])]
    ext_line = " ".join(php_ext)
    apt_line = " ".join(apt_pkgs)
    return f"""# syntax=docker/dockerfile:1
ARG PHP_VERSION={php}
ARG NODE_VERSION=22

FROM composer:2 AS vendor
WORKDIR /app
COPY composer.json composer.lock* ./
RUN composer install --no-dev --no-scripts --no-autoloader --prefer-dist --no-interaction --ignore-platform-reqs
COPY . .
RUN composer dump-autoload --optimize --classmap-authoritative --no-dev

# Laravel Wayfinder (and similar Vite plugins) run `php artisan` during `npm run build`.
FROM vendor AS assets
ARG NODE_VERSION=22
RUN apk add --no-cache nodejs npm
COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./
RUN if [ -f package.json ]; then \\
      if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \\
      else npm install --no-audit --no-fund; fi; \\
    fi
ENV APP_ENV=production \\
    APP_DEBUG=false \\
    APP_KEY=base64:2fl+KTV4PCFyLKaxT24KMW/Tprwcu7rg3UO5YfiQSyQ= \\
    DB_CONNECTION=sqlite \\
    DB_DATABASE=/tmp/build.sqlite \\
    CACHE_STORE=array \\
    SESSION_DRIVER=array \\
    QUEUE_CONNECTION=sync
RUN mkdir -p public/build storage/framework/cache storage/framework/sessions storage/framework/views storage/logs bootstrap/cache \\
    && touch /tmp/build.sqlite \\
    && if [ -f package.json ]; then npm run build; fi

# FrankenPHP speaks HTTP on :80. TLS stays on the VPS nginx + certbot edge.
FROM dunglas/frankenphp:php${{PHP_VERSION}} AS prod
WORKDIR /app
RUN install-php-extensions {ext_line} \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends {apt_line} \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
COPY .panel/Caddyfile /etc/frankenphp/Caddyfile
COPY .panel/entrypoint.sh /usr/local/bin/panel-entrypoint
RUN chmod +x /usr/local/bin/panel-entrypoint \\
    && for f in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml /etc/ImageMagick/policy.xml; do \\
         if [ -f "$f" ]; then sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/g' "$f"; fi; \\
       done
ENV SERVER_NAME=:80 \\
    COMPOSER_ALLOW_SUPERUSER=1
COPY --from=vendor /app /app
COPY --from=assets /app/public/build /app/public/build
RUN mkdir -p storage/framework/cache storage/framework/sessions storage/framework/views storage/logs bootstrap/cache \\
    && chown -R www-data:www-data storage bootstrap/cache
# FrankenPHP's base image probes Caddy's admin API on :2019. The panel Caddyfile
# turns that endpoint off. Probe Laravel /up (11+) and fall back to / (Laravel 10).
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \\
    CMD {APP_HTTP_HEALTHCHECK}
ENTRYPOINT ["panel-entrypoint"]
CMD ["frankenphp", "run", "--config", "/etc/frankenphp/Caddyfile"]
"""


# Host folders bind-mounted into the app. Keep these out of the image (GB of
# attachments) and off named volumes (panel backups / OneDrive read the host).
HOST_STORAGE_MOUNTS = (
    ("./storage/app", "/app/storage/app"),
    ("./storage/logs", "/app/storage/logs"),
    ("./public/uploads", "/app/public/uploads"),
)
# SQLite file lives here. Same rule as storage/: panel backups read the host path.
HOST_SQLITE_MOUNT = ("./database", "/app/database")
NAMED_STORAGE_VOLUME_MOUNTS = (
    "storage:/app/storage/app",
    "logs:/app/storage/logs",
    "database:/app/database",
)


def host_storage_volume_specs() -> list[str]:
    return [f"{src}:{dest}" for src, dest in HOST_STORAGE_MOUNTS]


def host_sqlite_volume_spec() -> str:
    src, dest = HOST_SQLITE_MOUNT
    return f"{src}:{dest}"


def ensure_host_storage_dirs(path: Path) -> None:
    for rel in ("storage/app", "storage/logs", "public/uploads", "database"):
        (path / rel).mkdir(parents=True, exist_ok=True)


def compose_uses_named_storage(compose: dict) -> bool:
    named = compose.get("volumes") or {}
    if "storage" in named or "logs" in named:
        return True
    for svc in (compose.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        for vol in svc.get("volumes") or []:
            if vol in NAMED_STORAGE_VOLUME_MOUNTS:
                return True
    return False


def dockerignore_text() -> str:
    # storage/ and uploads live on the host bind mount. Copying them into the
    # image bloats every layer (tens of GB if storage is copied in).
    # Keep .panel/Caddyfile + entrypoint for COPY in Dockerfile.panel; exclude
    # .panel/php so per-app ini stays host-only (bind-mounted at runtime).
    return "\n".join(
        [
            ".git",
            ".env",
            "docker-compose.yml",
            "docker-compose.panel.yml",
            ".panel/php",
            ".panel/php/**",
            ".panel-backups",
            "node_modules",
            "vendor",
            "storage",
            "storage/**",
            "public/storage",
            "public/hot",
            "public/uploads",
            "database/*.sqlite",
            "database/*.sqlite-*",
            "tests",
            ".phpunit.cache",
            ".phpunit.result.cache",
            "",
        ]
    )


def runtime_services_to_stop(addons: dict | None) -> list[str]:
    """App + workers. Leave database/redis running after a failed health check."""
    extras = normalize_addons(addons)
    names = ["app"]
    if extras.get("queue"):
        names.append("queue")
    if extras.get("scheduler"):
        names.append("scheduler")
    return names


def generate_compose(slug: str, addons: dict, network: str) -> dict:
    addons = normalize_addons(addons)
    image = f"panel-{slug}:latest"
    db = (addons.get("database") or "none").lower()
    redis_on = bool(addons.get("redis"))
    queue_on = bool(addons.get("queue"))
    scheduler_on = bool(addons.get("scheduler"))
    mailpit_on = bool(addons.get("mailpit"))

    app_env: dict[str, str] = {
        "APP_ENV": "production",
        "APP_DEBUG": "false",
        "SERVER_NAME": ":80",
    }
    volumes = host_storage_volume_specs()
    depends: dict[str, Any] = {}

    if db == "sqlite":
        volumes.append(host_sqlite_volume_spec())
    elif db == "mysql":
        depends["mysql"] = {"condition": "service_healthy"}
    elif db == "postgres":
        depends["postgres"] = {"condition": "service_healthy"}
    if redis_on:
        depends["redis"] = {"condition": "service_started"}

    services: dict[str, Any] = {
        "app": {
            "build": {
                "context": ".",
                "dockerfile": "Dockerfile.panel",
                "target": "prod",
            },
            "image": image,
            "container_name": f"{slug}_app",
            "restart": "unless-stopped",
            "env_file": ".env",
            "environment": app_env,
            "volumes": [
                *volumes,
                "./.panel/Caddyfile:/etc/frankenphp/Caddyfile:ro",
                PHP_INI_MOUNT,
            ],
            "networks": ["proxy", "internal"],
            # Override FrankenPHP's inherited :2019 admin probe. Admin is off.
            # /up is Laravel 11+; / works on Laravel 10 when /up 404s.
            "healthcheck": {
                "test": ["CMD-SHELL", APP_HTTP_HEALTHCHECK],
                "interval": "15s",
                "timeout": "5s",
                "start_period": "40s",
                "retries": 3,
            },
        }
    }
    if depends:
        services["app"]["depends_on"] = depends

    named_volumes: dict[str, dict] = {}

    if db == "mysql":
        services["mysql"] = {
            "image": "mysql:8.0",
            "container_name": f"{slug}_mysql",
            "restart": "unless-stopped",
            "environment": {
                "MYSQL_DATABASE": "${DB_DATABASE:-laravel}",
                "MYSQL_USER": "${DB_USERNAME:-laravel}",
                "MYSQL_PASSWORD": "${DB_PASSWORD}",
                "MYSQL_ROOT_PASSWORD": "${DB_ROOT_PASSWORD}",
            },
            "volumes": ["mysql_data:/var/lib/mysql"],
            # Internal only: short name `mysql` on the shared proxy network collides
            # across apps (same for postgres/redis). Panel DB access uses
            # `{slug}_mysql` via connect_dashboard_to_peer on this network.
            "networks": ["internal"],
            "healthcheck": {
                "test": ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1"],
                "interval": "5s",
                "timeout": "5s",
                "retries": 20,
            },
        }
        named_volumes["mysql_data"] = {}
    elif db == "postgres":
        services["postgres"] = {
            "image": "postgres:16-alpine",
            "container_name": f"{slug}_postgres",
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_DB": "${DB_DATABASE:-laravel}",
                "POSTGRES_USER": "${DB_USERNAME:-laravel}",
                "POSTGRES_PASSWORD": "${DB_PASSWORD}",
            },
            "volumes": ["postgres_data:/var/lib/postgresql/data"],
            # Internal only — see mysql note above (docs/AUDIT.md).
            "networks": ["internal"],
            "healthcheck": {
                "test": ["CMD-SHELL", POSTGRES_HEALTHCHECK],
                "interval": "5s",
                "timeout": "5s",
                "retries": 20,
            },
        }
        named_volumes["postgres_data"] = {}

    if redis_on:
        services["redis"] = {
            "image": "redis:7-alpine",
            "container_name": f"{slug}_redis",
            "restart": "unless-stopped",
            "command": ["redis-server", "--save", "60", "1"],
            "networks": ["internal"],
        }

    worker_common = {
        "image": image,
        "restart": "unless-stopped",
        "env_file": ".env",
        "environment": {"APP_ENV": "production", "APP_DEBUG": "false"},
        "volumes": [*volumes, PHP_INI_MOUNT],
        "depends_on": {"app": {"condition": "service_started"}},
        "networks": ["internal"],
    }
    if db == "mysql":
        worker_common["depends_on"]["mysql"] = {"condition": "service_healthy"}
    elif db == "postgres":
        worker_common["depends_on"]["postgres"] = {"condition": "service_healthy"}

    if queue_on:
        services["queue"] = {
            **worker_common,
            "container_name": f"{slug}_queue",
            "entrypoint": ["panel-entrypoint"],
            # Hourly --max-time recycle is intentional. Alerts ignore it (docs/AUDIT.md).
            "command": [
                "php",
                "artisan",
                "queue:work",
                "--tries=3",
                "--sleep=3",
                "--max-time=3600",
            ],
            "healthcheck": {"disable": True},
        }
    if scheduler_on:
        services["scheduler"] = {
            **worker_common,
            "container_name": f"{slug}_scheduler",
            "entrypoint": ["panel-entrypoint"],
            "command": ["php", "artisan", "schedule:work"],
            "healthcheck": {"disable": True},
        }
    if mailpit_on:
        services["mailpit"] = {
            "image": "axllent/mailpit:latest",
            "container_name": f"{slug}_mailpit",
            "restart": "unless-stopped",
            "networks": ["internal"],
            "environment": {"MP_MAX_MESSAGES": "5000"},
        }

    payload = {
        "name": panel_project_name(slug),
        "services": services,
        "networks": {
            "proxy": {"external": True, "name": network},
            "internal": {"driver": "bridge"},
        },
    }
    if named_volumes:
        payload["volumes"] = named_volumes
    return payload


def laravel_key() -> str:
    raw = secrets.token_bytes(32)
    import base64

    return "base64:" + base64.b64encode(raw).decode("ascii")


def env_values(slug: str, addons: dict, domain: str, existing: dict[str, str] | None = None) -> dict[str, str]:
    prev = existing or {}
    addons = normalize_addons(addons)
    db = (addons.get("database") or "none").lower()
    redis_on = bool(addons.get("redis"))
    queue_on = bool(addons.get("queue"))
    mailpit_on = bool(addons.get("mailpit"))
    url = f"https://{domain}" if domain else f"http://{slug}_app"
    password = prev.get("DB_PASSWORD") or secrets.token_urlsafe(18)
    root_password = prev.get("DB_ROOT_PASSWORD") or secrets.token_urlsafe(18)
    values = {
        "APP_NAME": prev.get("APP_NAME") or slug,
        "APP_ENV": "production",
        "APP_KEY": prev.get("APP_KEY") or laravel_key(),
        "APP_DEBUG": "false",
        "APP_URL": url,
        "LOG_CHANNEL": "stack",
        "LOG_LEVEL": "error",
    }
    if domain:
        existing_asset = (prev.get("ASSET_URL") or "").strip()
        values["ASSET_URL"] = existing_asset if existing_asset.startswith("https://") else url
    if db == "mysql":
        values.update(
            {
                "DB_CONNECTION": "mysql",
                "DB_HOST": "mysql",
                "DB_PORT": "3306",
                "DB_DATABASE": prev.get("DB_DATABASE") or "laravel",
                "DB_USERNAME": prev.get("DB_USERNAME") or "laravel",
                "DB_PASSWORD": password,
                "DB_ROOT_PASSWORD": root_password,
            }
        )
    elif db == "postgres":
        values.update(
            {
                "DB_CONNECTION": "pgsql",
                "DB_HOST": "postgres",
                "DB_PORT": "5432",
                "DB_DATABASE": prev.get("DB_DATABASE") or "laravel",
                "DB_USERNAME": prev.get("DB_USERNAME") or "laravel",
                "DB_PASSWORD": password,
            }
        )
    elif db == "sqlite":
        values.update(
            {
                "DB_CONNECTION": "sqlite",
                "DB_DATABASE": "/app/database/database.sqlite",
            }
        )
    elif prev.get("DB_CONNECTION") and prev.get("DB_HOST"):
        for key in (
            "DB_CONNECTION",
            "DB_HOST",
            "DB_PORT",
            "DB_DATABASE",
            "DB_USERNAME",
            "DB_PASSWORD",
        ):
            if prev.get(key):
                values[key] = prev[key]
    else:
        values["DB_CONNECTION"] = "sqlite"
        values["DB_DATABASE"] = "/app/database/database.sqlite"

    if redis_on:
        values.update(
            {
                "REDIS_CLIENT": "phpredis",
                "REDIS_HOST": "redis",
                "REDIS_PORT": "6379",
                "CACHE_STORE": "redis",
                "CACHE_DRIVER": "redis",
                "SESSION_DRIVER": "redis",
            }
        )
        if queue_on:
            values["QUEUE_CONNECTION"] = "redis"
    else:
        values.setdefault("CACHE_STORE", prev.get("CACHE_STORE") or "file")
        values.setdefault("CACHE_DRIVER", prev.get("CACHE_DRIVER") or "file")
        values.setdefault("SESSION_DRIVER", prev.get("SESSION_DRIVER") or "file")
        if queue_on:
            values["QUEUE_CONNECTION"] = "database"
        else:
            values["QUEUE_CONNECTION"] = "sync"

    if mailpit_on:
        values.update(
            {
                "MAIL_MAILER": "smtp",
                "MAIL_HOST": "mailpit",
                "MAIL_PORT": "1025",
                "MAIL_USERNAME": "",
                "MAIL_PASSWORD": "",
                "MAIL_ENCRYPTION": "",
                "MAIL_FROM_ADDRESS": prev.get("MAIL_FROM_ADDRESS") or f"hello@{slug}.local",
            }
        )
    return values


def write_runtime(
    path: Path,
    slug: str,
    addons: dict,
    domain: str,
    php: str,
    php_limits: dict | None = None,
) -> None:
    panel = path / ".panel"
    panel.mkdir(parents=True, exist_ok=True)
    (path / "Dockerfile.panel").write_text(dockerfile_text(php, addons))
    (path / ".dockerignore").write_text(dockerignore_text())
    caddy = TEMPLATES / "Caddyfile"
    entry = TEMPLATES / "entrypoint.sh"
    (panel / "Caddyfile").write_text(caddy.read_text())
    (panel / "entrypoint.sh").write_text(entry.read_text())
    (panel / "entrypoint.sh").chmod(0o755)
    write_php_ini(path, php_limits)

    ensure_host_storage_dirs(path)
    network = proxy_network_name()
    compose = generate_compose(slug, addons, network)
    (path / "docker-compose.panel.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
    )

    existing = parse_dotenv(path / ".env") if (path / ".env").exists() else {}
    values = env_values(slug, addons, domain, existing)
    write_dotenv(path / ".env", values, path / ".env.example")

    exclude = path / ".git" / "info" / "exclude"
    if (path / ".git").exists():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        extra = "\n".join(
            [
                "Dockerfile.panel",
                "docker-compose.panel.yml",
                ".dockerignore",
                ".panel/",
                ".panel-backups/",
                ".env",
            ]
        )
        current = exclude.read_text() if exclude.exists() else ""
        if "Dockerfile.panel" not in current:
            exclude.write_text(current.rstrip() + "\n" + extra + "\n")


_DB_ID_SUFFIXES = ("-postgres", "-mysql", "-db")


def panel_db_id(slug: str) -> str:
    return f"{slug}-db"


def app_id_from_db_id(conn_id: str) -> str | None:
    text = (conn_id or "").strip()
    for suffix in _DB_ID_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return None


def env_file_belongs_to_app(env_file: str, app_path: str) -> bool:
    if not env_file or not app_path:
        return False
    env = str(Path(env_file))
    root = str(Path(app_path))
    return env == str(Path(root) / ".env") or env.startswith(root.rstrip("/") + "/")


def stale_database_ids(app: dict, configs: list[dict]) -> list[str]:
    """Discovered DB connections left behind after switching to the panel template."""
    slug = app.get("id") or ""
    if not slug:
        return []
    canonical = panel_db_id(slug)
    app_path = str(app.get("path") or "")
    stale: list[str] = []
    for cfg in configs:
        cid = str(cfg.get("id") or "")
        if not cid or cid == canonical:
            continue
        engine = str(cfg.get("engine") or "").lower()
        if engine not in {"mysql", "mariadb", "postgres", "postgresql", "pgsql"}:
            continue
        if cid in {f"{slug}-postgres", f"{slug}-mysql"}:
            stale.append(cid)
            continue
        if env_file_belongs_to_app(str(cfg.get("env_file") or ""), app_path):
            stale.append(cid)
    return stale


def databases_entry(app: dict) -> dict | None:
    addons = app.get("addons") or {}
    db = (addons.get("database") or "none").lower()
    slug = app["id"]
    name = app.get("name") or slug
    env_file = str(Path(app["path"]) / ".env")
    env_map = {
        "password": "DB_PASSWORD",
        "user": "DB_USERNAME",
        "db": "DB_DATABASE",
    }
    if db == "mysql":
        return {
            "id": panel_db_id(slug),
            "name": f"{name} MySQL",
            "engine": "mysql",
            "host": f"{slug}_mysql",
            "port": 3306,
            "user": "laravel",
            "database": "laravel",
            "env_file": env_file,
            "env_map": env_map,
        }
    if db == "postgres":
        return {
            "id": panel_db_id(slug),
            "name": f"{name} PostgreSQL",
            "engine": "postgres",
            "host": f"{slug}_postgres",
            "port": 5432,
            "user": "laravel",
            "database": "laravel",
            "env_file": env_file,
            "env_map": env_map,
        }
    if db == "sqlite":
        return None
    return None


def upsert_database_config(app: dict) -> None:
    entry = databases_entry(app)
    if not entry:
        return
    settings = get_settings()
    configs = read_json_list(settings.databases_config_path)
    stale_ids = stale_database_ids(app, configs)
    drop = set(stale_ids) | {entry["id"]}
    configs = [c for c in configs if c.get("id") not in drop]
    configs.append(entry)
    write_json(settings.databases_config_path, configs)
    if stale_ids:
        from app.services import backup_svc

        backup_svc.repoint_connection_ids({old: entry["id"] for old in stale_ids})


def remove_database_config(app_id: str) -> None:
    settings = get_settings()
    configs = read_json_list(settings.databases_config_path)
    drop = {panel_db_id(app_id), f"{app_id}-postgres", f"{app_id}-mysql"}
    write_json(
        settings.databases_config_path,
        [c for c in configs if c.get("id") not in drop],
    )


def repoint_managed_database_targets() -> int:
    """Replace leftover discovered DBs with the live panel container and remount backup jobs."""
    from app.services import deploy_svc

    n = 0
    for app in deploy_svc.list_apps():
        if not app.get("managed"):
            continue
        settings = get_settings()
        before = read_json_list(settings.databases_config_path)
        stale = stale_database_ids(app, before)
        upsert_database_config(app)
        if stale:
            n += 1
    return n


def read_json_list(path: Path) -> list:
    data = read_json(path, [])
    return data if isinstance(data, list) else []
