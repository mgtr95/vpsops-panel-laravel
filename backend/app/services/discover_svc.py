"""Find compose apps, domains, and databases already running on this VPS."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.services import deploy_svc, docker_svc, proxy_svc
from app.utils import parse_dotenv, slugify, write_json

PG_CONN_ALIASES = {"pgsql", "postgres", "postgresql"}
PG_SERVICE_HINTS = ("postgres", "postgresql")
GENERIC_PG_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "postgres",
    "postgresql",
    "pgsql",
    "db",
    "database",
}
_COMPOSE_VAR_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)

SKIP_DIR_NAMES = {
    "vps-dashboard",
    "vps-ops-panel",
    "nginx",
    ".keep",
    "node_modules",
    ".git",
    "vendor",
    "vendor-bin",
}
SKIP_COMPOSE_FILES = {
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.https.yml",
    "docker-compose.https.yaml",
    "docker-compose.override.example.yml",
    "docker-compose.panel.yml",
    "docker-compose.panel.yaml",
}
SKIP_SERVICE_SETS = (
    {"dashboard", "edge", "rclone"},
    {"nginx", "certbot"},
)
DB_SERVICE_HINTS = ("mysql", "mariadb", "postgres", "postgresql", "redis", "memcached")
SERVER_NAME_RE = re.compile(r"server_name\s+([^;]+);", re.I)
BACKEND_RE = re.compile(
    r"(?:proxy_pass|fastcgi_pass|uwsgi_pass)\s+(?:https?://)?([^:/\s;$]+)",
    re.I,
)
ROOT_RE = re.compile(r"\broot\s+([^;]+);", re.I)

_lock = threading.Lock()


def sync(*, force: bool = False) -> dict[str, int]:
    """Register compose apps and databases found on disk / in Docker."""
    with _lock:
        apps_n = _merge_apps()
        dbs_n = _merge_databases()
        return {"apps": apps_n, "databases": dbs_n}


def _compose_dirs() -> list[Path]:
    settings = get_settings()
    dirs: dict[str, Path] = {}
    for root in settings.scan_paths():
        if not root.exists():
            continue
        for path in list(root.rglob("docker-compose*.yml")) + list(root.rglob("docker-compose*.yaml")):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.name in SKIP_COMPOSE_FILES or "example" in path.name:
                continue
            parent = path.parent
            dirs[str(parent)] = parent
    return sorted(dirs.values(), key=lambda p: str(p))


def _choose_compose(directory: Path) -> Path | None:
    for name in (
        "docker-compose.prod.yml",
        "docker-compose.prod.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
    ):
        path = directory / name
        if path.is_file():
            return path
    return None


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_panel_or_proxy(services: dict, directory: Path) -> bool:
    names = {str(k).lower() for k in services}
    for skip in SKIP_SERVICE_SETS:
        if skip.issubset(names):
            return True
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        cname = str(svc.get("container_name") or "").lower()
        if cname in {"vps_dashboard", "vps_nginx", "vps_certbot", "vps_rclone", "vps_dashboard_edge"}:
            return True
    if directory.name.lower() in SKIP_DIR_NAMES:
        return True
    return False


def _pretty(name: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", name) if part) or name


def _service_containers(services: dict) -> tuple[list[str], list[str]]:
    """Return (app-like container names, database container names)."""
    apps: list[str] = []
    dbs: list[str] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "").lower()
        cname = str(svc.get("container_name") or "").strip()
        hint = f"{svc_name} {image} {cname}".lower()
        is_db = any(h in hint for h in DB_SERVICE_HINTS)
        target = dbs if is_db else apps
        if cname:
            target.append(cname)
        else:
            target.append(svc_name)
    return apps, dbs


def _running_names_for_dir(directory: Path) -> list[str]:
    names: list[str] = []
    want = {directory.name.lower(), slugify(directory.name)}
    try:
        client = docker_svc.get_client()
        for c in client.containers.list(all=True):
            labels = (c.attrs.get("Config") or {}).get("Labels") or {}
            project = (labels.get("com.docker.compose.project") or "").lower()
            workdir = labels.get("com.docker.compose.project.working_dir") or ""
            if project in want or Path(workdir).name.lower() == directory.name.lower():
                names.append(c.name)
            elif workdir.rstrip("/").endswith("/" + directory.name):
                names.append(c.name)
    except Exception:
        pass
    return names


def _vhost_index() -> list[dict[str, Any]]:
    conf_dir = proxy_svc.conf_dir()
    if not conf_dir.is_dir():
        return []
    rows = []
    for path in sorted(conf_dir.glob("*.conf")):
        try:
            text = path.read_text()
        except OSError:
            continue
        domains: list[str] = []
        for match in SERVER_NAME_RE.findall(text):
            for part in match.split():
                host = part.strip().lower()
                if host and host not in {"_", "localhost"} and not host.startswith("www."):
                    domains.append(host)
                elif host.startswith("www.") and len(host) > 4:
                    domains.append(host[4:])
        backends = [b.strip() for b in BACKEND_RE.findall(text) if b.strip()]
        roots = [r.strip() for r in ROOT_RE.findall(text)]
        rows.append(
            {
                "file": path.stem,
                "domains": list(dict.fromkeys(domains)),
                "backends": backends,
                "roots": roots,
            }
        )
    return rows


def _domain_for(app_id: str, directory: Path, containers: list[str]) -> str:
    names = {app_id.lower(), directory.name.lower(), *(n.lower() for n in containers)}
    for row in _vhost_index():
        if row["file"].lower() in names:
            return (row["domains"] or [""])[0]
        if any(b.lower() in names for b in row["backends"]):
            return (row["domains"] or [""])[0]
        if any(directory.name.lower() in r.lower() for r in row["roots"]):
            return (row["domains"] or [""])[0]
    return ""


def discover_apps() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for directory in _compose_dirs():
        compose = _choose_compose(directory)
        if compose is None:
            continue
        data = _load_yaml(compose)
        services = data.get("services") or {}
        if not isinstance(services, dict) or _is_panel_or_proxy(services, directory):
            continue
        app_id = slugify(directory.name)
        app_containers, _db_containers = _service_containers(services)
        running = _running_names_for_dir(directory)
        restart = app_containers or [n for n in running if "mysql" not in n.lower() and "redis" not in n.lower()]
        has_git = (directory / ".git").is_dir()
        artisan = (directory / "artisan").exists()
        row = {
            "id": app_id,
            "name": _pretty(directory.name),
            "path": str(directory),
            "compose_file": compose.name,
            "pull": has_git,
            "rebuild_cmd": ["docker", "compose", "up", "-d", "--build"],
            "restart_containers": restart,
            "domain": _domain_for(app_id, directory, restart + running),
            "source": "discovered",
            "kind": "laravel" if artisan else "compose",
            "discovered": True,
        }
        env_file = deploy_svc.detect_compose_env_file(directory, compose.name)
        if env_file:
            row["compose_env_file"] = env_file
        found.append(row)
    for directory in _laravel_dirs_without_compose():
        app_id = slugify(directory.name)
        if any(str(Path(r["path"])) == str(directory) for r in found):
            continue
        found.append(
            {
                "id": app_id,
                "name": _pretty(directory.name),
                "path": str(directory),
                "compose_file": "",
                "pull": (directory / ".git").is_dir(),
                "rebuild_cmd": [],
                "restart_containers": [f"{app_id}_app"],
                "domain": _domain_for(app_id, directory, []),
                "source": "discovered",
                "kind": "laravel",
                "discovered": True,
                "needs_template": True,
            }
        )
    return found


def _laravel_dirs_without_compose() -> list[Path]:
    settings = get_settings()
    out: list[Path] = []
    for root in settings.scan_paths():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in SKIP_DIR_NAMES:
                continue
            if any(part in SKIP_DIR_NAMES for part in child.parts):
                continue
            if not (child / "artisan").is_file() or not (child / "composer.json").is_file():
                continue
            if _choose_compose(child) is not None:
                continue
            out.append(child)
    return out


def _ignored_path(path: Path | str) -> bool:
    return any(part in SKIP_DIR_NAMES for part in Path(path).parts)


def _is_stale_discovered(app: dict) -> bool:
    """True when a disk-discovered app's folder is gone or should not be listed."""
    if app.get("source") == "github" or app.get("managed"):
        return False
    path = app.get("path")
    if not path:
        return False
    if app.get("source") == "local":
        return not Path(path).is_dir()
    if app.get("source") not in (None, "", "discovered") and not app.get("discovered"):
        return False
    return (not Path(path).is_dir()) or _ignored_path(path)


def _merge_apps() -> int:
    existing = list(deploy_svc.list_apps())
    by_id = {a.get("id"): a for a in existing if a.get("id")}
    by_path = {str(Path(a["path"])): a for a in existing if a.get("path")}
    added = 0
    for found in discover_apps():
        path_key = str(Path(found["path"]))
        current = by_path.get(path_key) or by_id.get(found["id"])
        if current and (current.get("source") == "github" or current.get("managed")):
            continue
        if current:
            saved_github = current.get("github")
            if current.get("source") in (None, "", "discovered") or current.get("discovered"):
                if found.get("restart_containers"):
                    current["restart_containers"] = found["restart_containers"] or current.get(
                        "restart_containers"
                    )
                if found.get("compose_file"):
                    current["compose_file"] = current.get("compose_file") or found["compose_file"]
                if found.get("kind") and not current.get("managed"):
                    current["kind"] = current.get("kind") or found["kind"]
                if found.get("needs_template") and not current.get("managed"):
                    current["needs_template"] = True
                    current["kind"] = current.get("kind") or "laravel"
                if not current.get("compose_env_file") and found.get("compose_env_file"):
                    current["compose_env_file"] = found["compose_env_file"]
                if not current.get("domain") and found.get("domain"):
                    current["domain"] = found["domain"]
                current["discovered"] = True
                if not current.get("source"):
                    current["source"] = "discovered"
                if saved_github:
                    current["github"] = saved_github
            continue
        existing.append(found)
        by_id[found["id"]] = found
        by_path[path_key] = found
        added += 1
    kept = [a for a in existing if not _is_stale_discovered(a)]
    removed = len(existing) - len(kept)
    if added or removed or any(a.get("discovered") for a in kept):
        deploy_svc.save_apps(kept)
    return added


def _expand_compose_value(value: str, env: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1) or match.group(3)
        default = match.group(2)
        if key in env:
            return env[key]
        if default is not None:
            return default
        return match.group(0)

    return _COMPOSE_VAR_RE.sub(repl, value)


def _load_dir_env(directory: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (".env", ".env.prod", ".env.local"):
        path = directory / name
        if not path.is_file():
            continue
        for key, value in parse_dotenv(path).items():
            env.setdefault(key, value)
    return env


def _raw_service_env(svc: dict) -> dict[str, str]:
    block = svc.get("environment") or {}
    out: dict[str, str] = {}
    if isinstance(block, dict):
        for key, value in block.items():
            if value is None:
                continue
            out[str(key)] = str(value)
    elif isinstance(block, list):
        for item in block:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, _, value = item.partition("=")
            out[key.strip()] = value
    return out


def _service_env(svc: dict, file_env: dict[str, str]) -> dict[str, str]:
    return {k: _expand_compose_value(v, file_env) for k, v in _raw_service_env(svc).items()}


def _is_postgres_service(svc_name: str, svc: dict) -> bool:
    image = str(svc.get("image") or "").lower()
    cname = str(svc.get("container_name") or "").strip()
    return any(h in f"{svc_name} {image} {cname}".lower() for h in PG_SERVICE_HINTS)


def _postgres_containers(services: dict) -> list[str]:
    names: list[str] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if _is_postgres_service(svc_name, svc):
            names.append(str(svc.get("container_name") or svc_name).strip())
    return [n for n in names if n]


def _first_app_service(services: dict) -> dict | None:
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "").lower()
        cname = str(svc.get("container_name") or "").strip()
        hint = f"{svc_name} {image} {cname}".lower()
        if any(h in hint for h in DB_SERVICE_HINTS):
            continue
        return svc
    return None


def _merged_postgres_env(directory: Path, services: dict) -> dict[str, str]:
    file_env = _load_dir_env(directory)
    merged = dict(file_env)
    pg_svc = None
    for svc_name, svc in services.items():
        if isinstance(svc, dict) and _is_postgres_service(svc_name, svc):
            pg_svc = svc
            break
    if pg_svc:
        pg_env = _service_env(pg_svc, file_env)
        if pg_env.get("POSTGRES_DB"):
            merged.setdefault("DB_DATABASE", pg_env["POSTGRES_DB"])
        if pg_env.get("POSTGRES_USER"):
            merged.setdefault("DB_USERNAME", pg_env["POSTGRES_USER"])
        if pg_env.get("POSTGRES_PASSWORD"):
            merged.setdefault("DB_PASSWORD", pg_env["POSTGRES_PASSWORD"])
        if pg_env.get("POSTGRES_PORT"):
            merged.setdefault("DB_PORT", pg_env["POSTGRES_PORT"])
    app_svc = _first_app_service(services)
    if app_svc:
        for key, value in _service_env(app_svc, file_env).items():
            if key.startswith("DB_"):
                merged[key] = value
    return merged


def _postgres_password_binding(directory: Path, services: dict) -> tuple[str | None, str | None]:
    laravel = directory / ".env"
    if laravel.is_file() and parse_dotenv(laravel).get("DB_PASSWORD"):
        return str(laravel), "DB_PASSWORD"
    raw = ""
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        env = _raw_service_env(svc)
        for key in ("DB_PASSWORD", "POSTGRES_PASSWORD"):
            value = env.get(key) or ""
            if "${" in value or (value.startswith("$") and value[1:].replace("_", "").isalnum()):
                raw = value
                break
        if raw:
            break
    match = _COMPOSE_VAR_RE.search(raw or "")
    key = (match.group(1) or match.group(3)) if match else None
    if key:
        for name in (".env", ".env.prod", ".env.local"):
            path = directory / name
            if path.is_file() and parse_dotenv(path).get(key):
                return str(path), key
    return None, None


def _mysql_entry(app: dict, services: dict) -> dict | None:
    env_path = Path(app["path"]) / ".env"
    env = parse_dotenv(env_path) if env_path.exists() else {}
    conn = (env.get("DB_CONNECTION") or "").lower()
    db_containers = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "").lower()
        cname = str(svc.get("container_name") or "").strip()
        if any(h in f"{svc_name} {image} {cname}".lower() for h in ("mysql", "mariadb")):
            db_containers.append(cname or svc_name)
    if conn and conn not in ("mysql", "mariadb") and not db_containers:
        return None
    if not db_containers and conn not in ("mysql", "mariadb"):
        return None
    host = env.get("DB_HOST") or (db_containers[0] if db_containers else "")
    if host.lower() in {"127.0.0.1", "localhost", "::1", "mysql", "mariadb", "db", "database"}:
        host = db_containers[0] if db_containers else host
    if not host or not env_path.exists():
        return None
    if not (env.get("DB_DATABASE") or env.get("DB_USERNAME") or env.get("DB_PASSWORD")):
        return None
    return {
        "id": f"{app['id']}-mysql",
        "name": f"{app.get('name') or app['id']} MySQL",
        "engine": "mysql",
        "host": host,
        "port": int(env.get("DB_PORT") or 3306),
        "user": env.get("DB_USERNAME") or env.get("MYSQL_USER") or "app",
        "database": env.get("DB_DATABASE") or env.get("MYSQL_DATABASE") or "",
        "env_file": str(env_path),
        "env_map": {
            "host": "DB_HOST",
            "port": "DB_PORT",
            "user": "DB_USERNAME",
            "password": "DB_PASSWORD",
            "db": "DB_DATABASE",
        },
        "discovered": True,
    }


def _postgres_entry(app: dict, services: dict) -> dict | None:
    directory = Path(app["path"])
    env = _merged_postgres_env(directory, services)
    conn = (env.get("DB_CONNECTION") or "").lower()
    db_containers = _postgres_containers(services)
    if conn and conn not in PG_CONN_ALIASES and not db_containers:
        return None
    if not db_containers and conn not in PG_CONN_ALIASES:
        return None
    host = env.get("DB_HOST") or (db_containers[0] if db_containers else "")
    if host.lower() in GENERIC_PG_HOSTS:
        host = db_containers[0] if db_containers else host
    env_file, password_key = _postgres_password_binding(directory, services)
    if not host or not env_file or not password_key:
        return None
    if not (env.get("DB_DATABASE") or env.get("DB_USERNAME") or env.get("POSTGRES_DB")):
        return None
    laravel_env = directory / ".env"
    use_laravel_map = (
        Path(env_file) == laravel_env
        and password_key == "DB_PASSWORD"
        and bool(parse_dotenv(laravel_env).get("DB_PASSWORD"))
    )
    env_map = (
        {
            "host": "DB_HOST",
            "port": "DB_PORT",
            "user": "DB_USERNAME",
            "password": "DB_PASSWORD",
            "db": "DB_DATABASE",
        }
        if use_laravel_map
        else {"password": password_key}
    )
    return {
        "id": f"{app['id']}-postgres",
        "name": f"{app.get('name') or app['id']} PostgreSQL",
        "engine": "postgres",
        "host": host,
        "port": int(env.get("DB_PORT") or 5432),
        "user": env.get("DB_USERNAME") or env.get("POSTGRES_USER") or "app",
        "database": env.get("DB_DATABASE") or env.get("POSTGRES_DB") or "",
        "env_file": env_file,
        "env_map": env_map,
        "discovered": True,
    }


def _sqlite_entry(app: dict, services: dict | None = None) -> dict | None:
    env_path = Path(app["path"]) / ".env"
    env = parse_dotenv(env_path) if env_path.exists() else {}
    conn = (env.get("DB_CONNECTION") or "").lower()
    raw = (env.get("DB_DATABASE") or "").strip()
    if conn and conn != "sqlite":
        return None
    if raw.startswith("/app/") or raw.startswith("/var/"):
        return None
    if _sqlite_in_volume(services or {}):
        return None
    candidates: list[Path] = []
    if raw:
        p = Path(raw)
        candidates.append(p if p.is_absolute() else Path(app["path"]) / p)
    elif conn == "sqlite":
        candidates.extend(
            [
                Path(app["path"]) / "database" / "database.sqlite",
                Path(app["path"]) / "database.sqlite",
            ]
        )
    for path in candidates:
        if path.is_file():
            return {
                "id": f"{app['id']}-sqlite",
                "name": f"{app.get('name') or app['id']} SQLite",
                "engine": "sqlite",
                "path": str(path),
                "discovered": True,
            }
    return None


def _sqlite_in_volume(services: dict) -> bool:
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for vol in svc.get("volumes") or []:
            text = vol if isinstance(vol, str) else str((vol or {}).get("target") or "")
            if ":/app/database" in text or text.startswith("database:"):
                return True
    return False


def app_database_context(app: dict) -> dict[str, Any]:
    """Compose services plus the first discovered DB entry for an app folder."""
    directory = Path(app.get("path") or "")
    compose = _choose_compose(directory) if directory.is_dir() else None
    services = (_load_yaml(compose).get("services") or {}) if compose else {}
    if not isinstance(services, dict):
        services = {}
    entry = (
        _mysql_entry(app, services)
        or _postgres_entry(app, services)
        or _sqlite_entry(app, services)
    )
    return {
        "compose": str(compose) if compose else None,
        "services": services,
        "entry": entry,
    }


def database_entry_for_app(app: dict) -> dict | None:
    return app_database_context(app).get("entry")


def discover_databases() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for app in deploy_svc.list_apps():
        if app.get("managed"):
            continue
        directory = Path(app.get("path") or "")
        if not directory.is_dir():
            continue
        ctx = app_database_context(app)
        entry = ctx.get("entry")
        if entry:
            found.append(entry)
    by_id = {item["id"]: item for item in found}
    return list(by_id.values())


def _merge_databases() -> int:
    from app.utils import read_json
    from app.services import laravel_svc

    laravel_svc.repoint_managed_database_targets()

    settings = get_settings()
    path = settings.databases_config_path
    existing = read_json(path, []) or []
    if not isinstance(existing, list):
        existing = []
    by_id = {c.get("id"): c for c in existing if c.get("id")}
    by_env = {c.get("env_file"): c for c in existing if c.get("env_file")}
    by_sqlite = {c.get("path"): c for c in existing if c.get("engine") == "sqlite" and c.get("path")}
    added = 0
    for found in discover_databases():
        if found["id"] in by_id:
            continue
        slug = laravel_svc.app_id_from_db_id(found.get("id") or "")
        if slug and laravel_svc.panel_db_id(slug) in by_id:
            continue
        if found.get("env_file") and found["env_file"] in by_env:
            continue
        if found.get("engine") == "sqlite" and found.get("path") in by_sqlite:
            continue
        existing.append(found)
        by_id[found["id"]] = found
        added += 1
    if added:
        write_json(path, existing)
    return added


def running_map() -> dict[str, bool]:
    try:
        names = {c["name"] for c in docker_svc.list_containers(True) if c.get("status") == "running"}
    except Exception:
        return {}
    out = {}
    for app in deploy_svc.list_apps():
        watched = app.get("restart_containers") or []
        out[app["id"]] = any(n in names for n in watched) if watched else False
    return out
