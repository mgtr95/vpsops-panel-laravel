"""Backup and copy app data when switching an existing Laravel app onto the panel template."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services import db_svc, deploy_svc, docker_svc, laravel_svc
from app.utils import parse_dotenv

BACKUP_DIR_NAME = ".panel-backups"
SKIP_COPY_TABLES = {"migrations"}
ENGINE_LABELS = {
    "mysql": "MySQL / MariaDB",
    "postgres": "PostgreSQL",
    "sqlite": "SQLite",
}
DEFINER_RE = re.compile(
    r"DEFINER\s*=\s*(?:`[^`]+`@`[^`]+`|'[^']+'@'[^']+'|\"[^\"]+\"@\"[^\"]+\")\s*",
    re.IGNORECASE,
)
DB_SERVICE_HINTS = ("mysql", "mariadb", "postgres", "postgresql")
SAFE_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
DEFAULT_CONTAINER_SQLITE = "/app/database/database.sqlite"
# Broader paths first so a later /public copy can overlay files that lived
# only in a volume mounted on storage/app/public.
OLD_STORAGE_SOURCES = (
    "/app/storage/app",
    "/var/www/html/storage/app",
    "/app/storage/app/public",
    "/var/www/html/storage/app/public",
    "/app/storage/logs",
    "/var/www/html/storage/logs",
    "/app/public/uploads",
)


LogFn = Callable[[str], None]


def same_engine(source: str | None, target: str | None) -> bool:
    return _norm_engine(source) == _norm_engine(target) and bool(_norm_engine(source))


def _norm_engine(value: str | None) -> str:
    engine = (value or "").strip().lower()
    if engine in {"mysql", "mariadb"}:
        return "mysql"
    if engine in {"pgsql", "postgres", "postgresql"}:
        return "postgres"
    if engine == "sqlite":
        return "sqlite"
    return ""


def is_container_sqlite_path(path: str | None) -> bool:
    text = (path or "").strip()
    return text.startswith("/app/") or text.startswith("/var/www/")


def sqlite_writer_names(container: str) -> list[str]:
    """App + workers that share the panel SQLite volume."""
    if not container:
        return []
    if container.endswith("_app"):
        slug = container[: -len("_app")]
        return [f"{slug}_app", f"{slug}_queue", f"{slug}_scheduler"]
    return [container]


def storage_dest_for_source(src_path: str) -> str:
    text = src_path.rstrip("/")
    if text.endswith("uploads"):
        return "/app/public/uploads"
    if text.endswith("/public"):
        return "/app/storage/app/public"
    if text.endswith("storage/logs") or text.endswith("/logs"):
        return "/app/storage/logs"
    if text.endswith("/database") or text.endswith("database"):
        return "/app/database"
    return "/app/storage/app"


def storage_host_dir_for_source(app_path: Path, src_path: str) -> Path:
    text = src_path.rstrip("/")
    if text.endswith("uploads"):
        return app_path / "public" / "uploads"
    if text.endswith("/public"):
        return app_path / "storage" / "app" / "public"
    if text.endswith("storage/logs") or text.endswith("/logs"):
        return app_path / "storage" / "logs"
    if text.endswith("/database") or text.endswith("database"):
        return app_path / "database"
    return app_path / "storage" / "app"


def sqlite_consistent_copy(src: Path, dest: Path) -> None:
    """Copy a SQLite database including WAL via the backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    try:
        out = sqlite3.connect(str(dest), timeout=60)
        try:
            conn.backup(out)
            out.commit()
        finally:
            out.close()
    finally:
        conn.close()


def summarize_source(app: dict) -> dict | None:
    snap = snapshot_source(app, include_secrets=False)
    if not snap:
        return None
    engine = snap.get("engine") or ""
    return {
        "engine": engine,
        "label": ENGINE_LABELS.get(engine, engine or "database"),
        "database": snap.get("database") or "",
        "host": snap.get("host") or "",
        "path": snap.get("path") or "",
        "container": snap.get("container") or "",
    }


def snapshot_source(app: dict, *, include_secrets: bool = True) -> dict | None:
    from app.services import discover_svc

    path = Path(app.get("path") or "")
    if not path.is_dir():
        return None
    ctx = discover_svc.app_database_context(app)
    entry = ctx.get("entry")
    services = ctx.get("services") or {}
    env = parse_dotenv(path / ".env") if (path / ".env").exists() else {}
    if not entry:
        conn = (env.get("DB_CONNECTION") or "").lower()
        if conn in {"mysql", "mariadb", "pgsql", "postgres", "postgresql", "sqlite"}:
            entry = {
                "engine": "mysql"
                if conn in {"mysql", "mariadb"}
                else ("postgres" if conn != "sqlite" else "sqlite"),
                "host": env.get("DB_HOST") or "",
                "port": int(env.get("DB_PORT") or (5432 if conn not in {"mysql", "mariadb", "sqlite"} else 3306)),
                "user": env.get("DB_USERNAME") or "",
                "database": env.get("DB_DATABASE") or "",
                "path": str(path / env["DB_DATABASE"])
                if conn == "sqlite" and env.get("DB_DATABASE") and not str(env.get("DB_DATABASE")).startswith("/")
                else env.get("DB_DATABASE") or "",
            }
        else:
            return None

    engine = _norm_engine(entry.get("engine") or env.get("DB_CONNECTION"))
    if not engine:
        return None

    resolved: dict[str, Any] = {"engine": engine}
    if engine == "sqlite":
        sqlite_path = str(entry.get("path") or env.get("DB_DATABASE") or "").strip()
        container = _app_container_name(services)
        in_volume = discover_svc._sqlite_in_volume(services)
        if in_volume or is_container_sqlite_path(sqlite_path):
            sqlite_path = sqlite_path if is_container_sqlite_path(sqlite_path) else DEFAULT_CONTAINER_SQLITE
        elif sqlite_path and not Path(sqlite_path).is_absolute():
            sqlite_path = str(path / sqlite_path)
        resolved["path"] = sqlite_path
        resolved["container"] = container
        if include_secrets:
            resolved["password"] = ""
        return resolved

    cfg = dict(entry)
    if not cfg.get("env_file") and (path / ".env").exists():
        cfg["env_file"] = str(path / ".env")
    data = db_svc._resolve_mysql(cfg) if engine == "mysql" else db_svc._resolve_postgres(cfg)
    if not data.get("password") and env.get("DB_PASSWORD"):
        data["password"] = env["DB_PASSWORD"]
    if not data.get("db") and not data.get("database"):
        data["db"] = env.get("DB_DATABASE") or ""

    container = _db_container_name(services, engine, data.get("host") or "")
    resolved.update(
        {
            "host": data.get("host") or "",
            "port": int(data.get("port") or (3306 if engine == "mysql" else 5432)),
            "user": data.get("user") or "",
            "database": data.get("db") or data.get("database") or "",
            "container": container,
        }
    )
    if include_secrets:
        resolved["password"] = data.get("password") or ""
        root_user, root_password = _compose_root_secrets(services, engine)
        resolved["root_user"] = root_user
        resolved["root_password"] = root_password
    return resolved


def dest_snapshot(app: dict, *, include_secrets: bool = True) -> dict | None:
    addons = laravel_svc.normalize_addons(app.get("addons") or {})
    engine = _norm_engine(addons.get("database"))
    if not engine:
        return None
    slug = app["id"]
    path = Path(app.get("path") or "")
    env = parse_dotenv(path / ".env") if (path / ".env").exists() else {}
    if engine == "sqlite":
        return {
            "engine": "sqlite",
            "path": env.get("DB_DATABASE") or "/app/database/database.sqlite",
            "container": f"{slug}_app",
        }
    host = f"{slug}_mysql" if engine == "mysql" else f"{slug}_postgres"
    snap = {
        "engine": engine,
        "host": host,
        "port": 3306 if engine == "mysql" else 5432,
        "user": env.get("DB_USERNAME") or "laravel",
        "database": env.get("DB_DATABASE") or "laravel",
        "container": host,
    }
    if include_secrets:
        snap["password"] = env.get("DB_PASSWORD") or ""
        snap["root_user"] = "root" if engine == "mysql" else (env.get("DB_USERNAME") or "laravel")
        snap["root_password"] = env.get("DB_ROOT_PASSWORD") or env.get("DB_PASSWORD") or ""
    return snap


def public_source_cfg(snap: dict) -> dict:
    out = {k: v for k, v in snap.items() if k not in {"password", "root_password"}}
    return out


def strip_definers(sql: str) -> str:
    return DEFINER_RE.sub("", sql)


def new_backup_dir(app_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = app_path / BACKUP_DIR_NAME / stamp
    dest.mkdir(parents=True, exist_ok=True)
    try:
        dest.chmod(0o700)
    except OSError:
        pass
    return dest


async def backup_existing_database(app: dict, log: LogFn) -> dict:
    """Dump the current app database onto the app disk. Does not change running containers."""
    path = Path(app["path"])
    backup_dir = new_backup_dir(path)
    snap = snapshot_source(app, include_secrets=True)
    manifest = {
        "app_id": app.get("id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": public_source_cfg(snap) if snap else None,
        "storage_untouched": True,
        "storage_paths": _existing_storage_paths(path),
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if snap:
        (backup_dir / "source.json").write_text(json.dumps(snap, indent=2) + "\n")
        try:
            (backup_dir / "source.json").chmod(0o600)
        except OSError:
            pass

    if not snap:
        log("No database found on this app. Wrote an empty backup folder for the record.\n")
        return {"dir": str(backup_dir), "source": None, "dump": None}

    engine = snap["engine"]
    dump_path = backup_dir / ("database.sqlite" if engine == "sqlite" else "database.sql")
    log(
        f"Backing up {ENGINE_LABELS.get(engine, engine)} "
        f"{snap.get('database') or snap.get('path') or ''} locally to {backup_dir}\n"
    )
    await _dump_source(snap, dump_path, log)
    if not dump_path.exists() or dump_path.stat().st_size == 0:
        raise RuntimeError("Database backup produced an empty file")
    log(f"Wrote {dump_path.name} ({dump_path.stat().st_size} bytes). Host storage/ was not modified.\n")
    return {"dir": str(backup_dir), "source": snap, "dump": str(dump_path)}


async def materialize_storage_on_host(app: dict, log: LogFn) -> None:
    """Copy live container/volume files onto the host folder compose bind-mounts.

    Named volumes hid uploads from OneDrive and panel file backups, which read
    `{app}/storage/` on disk. Skip when the mount is already that host path.
    """
    from app.services.laravel_svc import (
        HOST_SQLITE_MOUNT,
        HOST_STORAGE_MOUNTS,
        ensure_host_storage_dirs,
    )

    path = Path(app["path"])
    ensure_host_storage_dirs(path)
    container = f"{app['id']}_app"
    mounts = list(HOST_STORAGE_MOUNTS)
    addons = app.get("addons") or {}
    if str(addons.get("database") or "").lower() == "sqlite":
        mounts.append(HOST_SQLITE_MOUNT)
    copied = 0
    if _container_exists(container):
        for _src, dest in mounts:
            host_dir = storage_host_dir_for_source(path, dest)
            if _mount_is_host_bind(container, dest, host_dir):
                continue
            if not await _container_path_has_files(container, dest):
                continue
            host_dir.mkdir(parents=True, exist_ok=True)
            log(f"Copying {container}:{dest} → {host_dir} (live files onto the host folder)\n")
            await _docker_cp(f"{container}:{dest}/.", str(host_dir))
            copied += 1
    copied += await _copy_old_container_storage(app, log)
    if copied == 0:
        log("Host storage already matches the running app, or there were no files to copy.\n")
    else:
        log(f"Copied storage onto the host folder ({copied} source(s)).\n")


async def copy_storage_into_volumes(app: dict, log: LogFn) -> None:
    """Keep host storage as the live copy. Pull files out of containers/volumes."""
    slug = app["id"]
    container = f"{slug}_app"
    # HTTP /up often fails until data is imported. docker cp only needs the process running.
    await _wait_container(container, timeout=90, require_healthy=False)
    log("App container is running; syncing storage onto the host folder even if HTTP health is not green yet.\n")
    await materialize_storage_on_host(app, log)


async def restore_or_convert(app: dict, backup: dict, log: LogFn) -> dict:
    """Load backed-up data into the new database. Same engine = dump restore; otherwise convert."""
    dest = dest_snapshot(app, include_secrets=True)
    source = backup.get("source") or snapshot_source(app, include_secrets=True)
    dump = Path(backup["dump"]) if backup.get("dump") else None
    if not dest:
        log("New template has no database container (None). Existing DB is left running.\n")
        return {"restored_dump": False, "converted": False, "skipped": True}
    if not source:
        log("No source database to copy.\n")
        return {"restored_dump": False, "converted": False, "skipped": True}

    src_engine = source["engine"]
    dst_engine = dest["engine"]
    log(
        f"Migrating data {ENGINE_LABELS.get(src_engine, src_engine)} → "
        f"{ENGINE_LABELS.get(dst_engine, dst_engine)}\n"
    )
    await _wait_dest_db(dest)

    if same_engine(src_engine, dst_engine) and dump and dump.exists():
        try:
            await _wipe_dest_database(dest, log)
            await _restore_same_engine(source, dest, dump, log)
            return {"restored_dump": True, "converted": False, "skipped": False}
        except Exception as e:
            log(f"Dump restore failed ({e}). Falling back to table copy.\n")

    await _ensure_schema(app, log)
    await _copy_all_tables(source, dest, log)
    return {"restored_dump": False, "converted": True, "skipped": False}


async def stop_old_compose(app: dict, *, keep_database: bool, log: LogFn) -> None:
    cwd = Path(deploy_svc.compose_cwd(app))
    compose = _old_compose_file(cwd)
    if not compose:
        log("No previous docker-compose.yml to stop.\n")
        return
    if keep_database:
        names = _non_db_service_names(compose)
        if not names:
            log("Previous compose has no app services to stop; leaving the old database running.\n")
            return
        log(f"Stopping old app services {', '.join(names)} (database stays).\n")
        cmd = ["docker", "compose", "-f", compose.name, "stop", *names]
    else:
        log(f"Stopping previous stack from {compose.name} without deleting volumes.\n")
        cmd = ["docker", "compose", "-f", compose.name, "down"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    if text:
        log(text if text.endswith("\n") else text + "\n")
    if proc.returncode not in (0, None) and proc.returncode != 0:
        log(f"Old compose stop exited {proc.returncode} (continuing; new stack is already up).\n")


def _existing_storage_paths(path: Path) -> list[str]:
    found = []
    for rel in ("storage/app", "storage/logs", "public/uploads", "public/storage"):
        candidate = path / rel
        if candidate.exists():
            found.append(rel)
    return found


def _old_compose_file(directory: Path) -> Path | None:
    for name in (
        "docker-compose.prod.yml",
        "docker-compose.prod.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
    ):
        path = directory / name
        if path.is_file():
            return path
    return None


def _non_db_service_names(compose: Path) -> list[str]:
    import yaml

    try:
        data = yaml.safe_load(compose.read_text()) or {}
    except Exception:
        return []
    services = data.get("services") or {}
    names = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        blob = f"{name} {svc.get('image') or ''} {svc.get('container_name') or ''}".lower()
        if any(h in blob for h in DB_SERVICE_HINTS):
            continue
        names.append(str(name))
    return names


def _compose_env(svc: dict) -> dict[str, str]:
    raw = svc.get("environment") or {}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                out[key] = value
        return out
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    return {}


def _compose_root_secrets(services: dict, engine: str) -> tuple[str, str]:
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        blob = f"{svc.get('image') or ''} {svc.get('container_name') or ''}".lower()
        env = _compose_env(svc)
        if engine == "mysql" and any(h in blob for h in ("mysql", "mariadb")):
            return "root", env.get("MYSQL_ROOT_PASSWORD") or ""
        if engine == "postgres" and "postgres" in blob:
            return env.get("POSTGRES_USER") or "postgres", env.get("POSTGRES_PASSWORD") or ""
    return ("root" if engine == "mysql" else "postgres", "")


def _db_container_name(services: dict, engine: str, host: str) -> str:
    hints = ("mysql", "mariadb") if engine == "mysql" else ("postgres", "postgresql")
    names: list[str] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "").lower()
        cname = str(svc.get("container_name") or "").strip()
        if any(h in f"{svc_name} {image} {cname}".lower() for h in hints):
            names.append(cname or svc_name)
    if host and _container_running(host):
        return host
    for name in names:
        if _container_running(name):
            return name
    return names[0] if names else host


def _app_container_name(services: dict) -> str:
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        blob = f"{svc_name} {svc.get('image') or ''} {svc.get('container_name') or ''}".lower()
        if any(h in blob for h in DB_SERVICE_HINTS):
            continue
        cname = str(svc.get("container_name") or "").strip()
        if cname:
            return cname
        return str(svc_name)
    return ""


def _container_running(name: str) -> bool:
    if not name:
        return False
    try:
        client = docker_svc.get_client()
        container = client.containers.get(name)
        container.reload()
        return container.status == "running"
    except Exception:
        return False


def container_ready(status: str, health: str | None, *, require_healthy: bool) -> bool:
    """App HTTP health is not required to copy files or import SQL.

    During a template switch the new app is often `running/unhealthy` because
    Laravel `/up` checks the (still empty) database. The database container
    should still be healthy before a dump restore.
    """
    if (status or "").lower() != "running":
        return False
    if not require_healthy:
        return True
    return (health or "").lower() in {"", "healthy"}


async def _wait_container(name: str, timeout: int = 90, *, require_healthy: bool = True) -> None:
    if not name:
        raise RuntimeError("No container name to wait for")
    deadline = asyncio.get_event_loop().time() + timeout
    last = "not found"
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = docker_svc.get_client()
            container = client.containers.get(name)
            container.reload()
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
            status = container.status or ""
            if container_ready(status, health, require_healthy=require_healthy):
                return
            last = f"{status}/{health or 'no-health'}"
        except Exception as e:
            last = str(e)
        await asyncio.sleep(2)
    raise RuntimeError(f"Container {name} was not ready ({last})")


async def _wait_dest_db(dest: dict) -> None:
    if dest.get("engine") == "sqlite":
        await _wait_container(dest.get("container") or "", require_healthy=False)
        return
    await _wait_container(dest["container"], timeout=120, require_healthy=True)
    await asyncio.sleep(2)


async def _dump_source(snap: dict, dump_path: Path, log: LogFn) -> None:
    engine = snap["engine"]
    if engine == "sqlite":
        await _dump_sqlite(snap, dump_path, log)
        sql_copy = dump_path.with_suffix(".sql")
        if dump_path.is_file() and not sql_copy.exists():
            conn = sqlite3.connect(str(dump_path))
            try:
                sql_copy.write_text("\n".join(conn.iterdump()) + "\n")
            finally:
                conn.close()
        return

    container = snap.get("container") or ""
    if container and _container_running(container):
        try:
            await _dump_via_exec(snap, dump_path)
            return
        except Exception as e:
            log(f"Dump inside {container} failed ({e}); trying network dump.\n")
    cfg = _cfg_from_snap(snap)
    raw = await db_svc.dump_from_cfg(cfg, snap.get("database"))
    if engine == "mysql":
        raw = strip_definers(raw.decode("utf-8", errors="replace")).encode("utf-8")
    dump_path.write_bytes(raw)


async def _dump_via_exec(snap: dict, dump_path: Path) -> None:
    engine = snap["engine"]
    database = snap.get("database") or ""
    container = snap["container"]
    attempts: list[tuple[str, str]] = []
    if snap.get("user"):
        attempts.append((str(snap.get("user") or ""), str(snap.get("password") or "")))
    root_user = str(snap.get("root_user") or "")
    if root_user and (root_user, str(snap.get("root_password") or "")) not in attempts:
        attempts.append((root_user, str(snap.get("root_password") or "")))
    if not attempts:
        raise RuntimeError("No database user for dump")
    last_error = "dump failed"
    for user, password in attempts:
        try:
            if engine == "mysql":
                cmd = [
                    "docker",
                    "exec",
                    "-e",
                    f"MYSQL_PWD={password}",
                    container,
                    "mysqldump",
                    "-u",
                    user,
                    "--single-transaction",
                    "--quick",
                    "--no-tablespaces",
                    "--routines",
                    "--triggers",
                    "--set-charset",
                    database,
                ]
            else:
                cmd = [
                    "docker",
                    "exec",
                    "-e",
                    f"PGPASSWORD={password}",
                    container,
                    "pg_dump",
                    "-U",
                    user,
                    "-d",
                    database,
                    "--no-owner",
                    "--no-acl",
                ]
            await _run_to_file(cmd, dump_path)
            if engine == "mysql":
                text = dump_path.read_text(encoding="utf-8", errors="replace")
                dump_path.write_text(strip_definers(text), encoding="utf-8")
            return
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(last_error)


def postgres_wipe_sql() -> str:
    return (
        "DROP SCHEMA public CASCADE; "
        "CREATE SCHEMA public; "
        "GRANT ALL ON SCHEMA public TO PUBLIC;"
    )


def mysql_wipe_sql(database: str) -> str:
    name = (database or "").strip()
    if not SAFE_DB_NAME_RE.match(name):
        raise RuntimeError("Refusing to wipe a database with an unsafe name")
    return (
        f"DROP DATABASE IF EXISTS `{name}`; "
        f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    )


async def _wipe_dest_database(dest: dict, log: LogFn) -> None:
    """Empty the new database so a retry can restore the dump instead of hitting duplicate keys."""
    engine = dest.get("engine")
    if engine == "sqlite":
        log("New SQLite file will be replaced by the backup copy.\n")
        return
    container = dest.get("container") or ""
    if not container or not _container_running(container):
        log("New database container is not running; skip wipe and let restore handle an empty schema.\n")
        return
    log("Clearing the new database first (retries must not fail because the last attempt already imported data).\n")
    user = dest.get("root_user") or dest.get("user") or ""
    password = dest.get("root_password") or dest.get("password") or ""
    database = dest.get("database") or ""
    if engine == "postgres":
        cmd = [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            postgres_wipe_sql(),
        ]
    elif engine == "mysql":
        cmd = [
            "docker",
            "exec",
            "-e",
            f"MYSQL_PWD={password}",
            container,
            "mysql",
            "-u",
            user,
            "-e",
            mysql_wipe_sql(database),
        ]
    else:
        return
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace") or f"wipe exited {proc.returncode}")


async def _restore_same_engine(source: dict, dest: dict, dump: Path, log: LogFn) -> None:
    engine = dest["engine"]
    if engine == "sqlite":
        await _restore_sqlite_file(dest, dump, log)
        return
    container = dest.get("container") or ""
    if container and _container_running(container):
        log(f"Importing dump into {container}.\n")
        await _import_via_exec(dest, dump)
        return
    cfg = _cfg_from_snap(dest, prefer_root=True)
    raw = dump.read_bytes()
    if engine == "mysql":
        raw = strip_definers(raw.decode("utf-8", errors="replace")).encode("utf-8")
    log("Importing dump over the Docker network.\n")
    await db_svc.import_to_cfg(cfg, raw, dest.get("database"))


async def _restore_sqlite_file(dest: dict, dump: Path, log: LogFn) -> None:
    container = dest.get("container") or ""
    remote = dest.get("path") or DEFAULT_CONTAINER_SQLITE
    src = dump if dump.suffix == ".sqlite" else None
    if src is None:
        tmp = dump.with_suffix(".sqlite")
        conn = sqlite3.connect(str(tmp))
        try:
            conn.executescript(dump.read_text(encoding="utf-8", errors="replace"))
            conn.commit()
        finally:
            conn.close()
        src = tmp
    await _restore_sqlite_into_container(container, remote, src, log)


async def _import_via_exec(dest: dict, dump: Path) -> None:
    engine = dest["engine"]
    user = dest.get("root_user") or dest.get("user") or ""
    password = dest.get("root_password") or dest.get("password") or ""
    database = dest.get("database") or ""
    container = dest["container"]
    if engine == "mysql":
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"MYSQL_PWD={password}",
            container,
            "mysql",
            "-u",
            user,
            database,
        ]
    else:
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
        ]
    with dump.open("rb") as fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=fh,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace") or f"import exited {proc.returncode}")


async def _ensure_schema(app: dict, log: LogFn) -> None:
    slug = app["id"]
    log("Creating schema on the new database with php artisan migrate --force\n")
    client = docker_svc.get_client()
    container = client.containers.get(f"{slug}_app")
    result = await asyncio.to_thread(
        container.exec_run, ["php", "artisan", "migrate", "--force", "--no-interaction"]
    )
    out = (result.output or b"").decode("utf-8", errors="replace") if result.output else ""
    if out:
        log(out if out.endswith("\n") else out + "\n")
    if result.exit_code != 0:
        raise RuntimeError("artisan migrate failed while preparing the new database")


async def _copy_all_tables(source: dict, dest: dict, log: LogFn) -> None:
    src_cfg = _cfg_from_snap(source)
    dst_cfg = _cfg_from_snap(dest)
    if dest.get("engine") == "sqlite":
        dst_cfg = await _sqlite_dest_cfg(dest)
    src_tables = await _table_names(src_cfg)
    dst_tables = await _table_names(dst_cfg)
    dst_set = {t.lower(): t for t in dst_tables}
    copied = 0
    skipped = 0
    errors: list[str] = []
    for table in src_tables:
        if table.lower() in SKIP_COPY_TABLES:
            log(f"Skipping {table} (schema already applied on the new database).\n")
            skipped += 1
            continue
        dest_table = dst_set.get(table.lower())
        if not dest_table:
            log(f"Source table {table} has no matching table on the new database; skipped.\n")
            skipped += 1
            continue
        try:
            n = await _copy_table(src_cfg, dst_cfg, table, dest_table)
            log(f"Copied {n} row(s) {table} → {dest_table}\n")
            copied += 1
        except Exception as e:
            msg = f"{table}: {e}"
            errors.append(msg)
            log(f"Could not copy {msg}\n")
    if dest.get("engine") == "sqlite":
        await _push_sqlite_dest(dest, dst_cfg.get("path") or "")
    elif dest.get("engine") == "postgres":
        await _reset_postgres_sequences(dst_cfg)
    log(f"Table copy finished: {copied} copied, {skipped} skipped.\n")
    if errors:
        raise RuntimeError("Some tables failed to copy: " + "; ".join(errors[:8]))


async def _copy_table(src_cfg: dict, dst_cfg: dict, src_table: str, dst_table: str) -> int:
    src_cols = await _column_names(src_cfg, src_table)
    dst_cols = await _column_names(dst_cfg, dst_table)
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        return 0
    rows = await _fetch_all(src_cfg, src_table, cols)
    if not rows:
        return 0
    await _empty_table(dst_cfg, dst_table)
    await _insert_rows(dst_cfg, dst_table, cols, rows)
    return len(rows)


async def _empty_table(cfg: dict, table: str) -> None:
    engine = _norm_engine(cfg.get("engine"))
    quoted = db_svc._quote(cfg, table)
    if engine == "mysql":
        pool = await db_svc._mysql_pool(cfg)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SET FOREIGN_KEY_CHECKS=0")
                    await cur.execute(f"TRUNCATE TABLE {quoted}")
                    await cur.execute("SET FOREIGN_KEY_CHECKS=1")
        finally:
            pool.close()
            await pool.wait_closed()
        return
    if engine == "postgres":
        db_conn = await db_svc._postgres_conn(cfg)
        try:
            async with db_conn.cursor() as cur:
                await cur.execute("SET session_replication_role = replica")
                await cur.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY")
                await cur.execute("SET session_replication_role = DEFAULT")
            await db_conn.commit()
        finally:
            await db_conn.close()
        return
    conn = _sqlite_open(cfg)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(f"DELETE FROM {quoted}")
        conn.commit()
    finally:
        conn.close()


async def _table_names(cfg: dict) -> list[str]:
    engine = _norm_engine(cfg.get("engine"))
    if engine == "mysql":
        pool = await db_svc._mysql_pool(cfg)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW TABLE STATUS")
                    rows = await cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    out = []
                    for row in rows:
                        item = dict(zip(cols, row))
                        if not item.get("Engine"):
                            continue
                        name = item.get("Name")
                        if name:
                            out.append(str(name))
                    return out
        finally:
            pool.close()
            await pool.wait_closed()
    if engine == "postgres":
        _cols, rows, _n = await db_svc._server_execute(
            cfg,
            cfg.get("database") or cfg.get("db"),
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """,
        )
        return [r[0] for r in rows]
    conn = _sqlite_open(cfg)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


async def _column_names(cfg: dict, table: str) -> list[str]:
    engine = _norm_engine(cfg.get("engine"))
    if engine == "mysql":
        pool = await db_svc._mysql_pool(cfg)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(f"SHOW COLUMNS FROM {db_svc._quote_mysql(table)}")
                    rows = await cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    names = []
                    for row in rows:
                        item = dict(zip(cols, row))
                        extra = str(item.get("Extra") or "").lower()
                        if "generated" in extra or extra.startswith("virtual"):
                            continue
                        names.append(str(item.get("Field")))
                    return names
        finally:
            pool.close()
            await pool.wait_closed()
    if engine == "postgres":
        _cols, rows, _n = await db_svc._server_execute(
            cfg,
            cfg.get("database") or cfg.get("db"),
            """
            SELECT a.attname
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            [table],
        )
        return [r[0] for r in rows]
    conn = _sqlite_open(cfg)
    try:
        cur = conn.execute(f"PRAGMA table_info({db_svc._quote_sqlite(table)})")
        return [r[1] for r in cur.fetchall()]
    finally:
        conn.close()


async def _fetch_all(cfg: dict, table: str, columns: list[str]) -> list[tuple[Any, ...]]:
    engine = _norm_engine(cfg.get("engine"))
    quoted_cols = ", ".join(db_svc._quote(cfg, c) for c in columns)
    sql = f"SELECT {quoted_cols} FROM {db_svc._quote(cfg, table)}"
    if engine in {"mysql", "postgres"}:
        _cols, rows, _n = await db_svc._server_execute(cfg, cfg.get("database") or cfg.get("db"), sql)
        return [tuple(rows_row) for rows_row in rows]
    conn = _sqlite_open(cfg)
    try:
        cur = conn.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


async def _insert_rows(cfg: dict, table: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    engine = _norm_engine(cfg.get("engine"))
    quoted_cols = ", ".join(db_svc._quote(cfg, c) for c in columns)
    ph = db_svc._ph(cfg)
    placeholders = ", ".join([ph] * len(columns))
    sql = f"INSERT INTO {db_svc._quote(cfg, table)} ({quoted_cols}) VALUES ({placeholders})"
    converted = [_adapt_row(engine, row) for row in rows]
    if engine == "mysql":
        pool = await db_svc._mysql_pool(cfg)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SET FOREIGN_KEY_CHECKS=0")
                    await cur.executemany(sql, converted)
                    await cur.execute("SET FOREIGN_KEY_CHECKS=1")
        finally:
            pool.close()
            await pool.wait_closed()
        return
    if engine == "postgres":
        db_conn = await db_svc._postgres_conn(cfg)
        try:
            async with db_conn.cursor() as cur:
                await cur.execute("SET session_replication_role = replica")
                await cur.executemany(sql, converted)
                await cur.execute("SET session_replication_role = DEFAULT")
            await db_conn.commit()
        finally:
            await db_conn.close()
        return
    conn = _sqlite_open(cfg)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executemany(sql, converted)
        conn.commit()
    finally:
        conn.close()


def _adapt_row(engine: str, row: tuple[Any, ...]) -> tuple[Any, ...]:
    out = []
    for cell in row:
        if cell is None:
            out.append(None)
            continue
        if isinstance(cell, (bytes, memoryview, bytearray)):
            out.append(bytes(cell))
            continue
        if engine == "postgres" and isinstance(cell, (dict, list)):
            out.append(json.dumps(cell, default=str))
            continue
        if engine != "postgres" and isinstance(cell, (dict, list)):
            out.append(json.dumps(cell, default=str))
            continue
        out.append(cell)
    return tuple(out)


async def _reset_postgres_sequences(cfg: dict) -> None:
    sql = """
    SELECT n.nspname, c.relname, a.attname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
      AND pg_get_serial_sequence(n.nspname || '.' || c.relname, a.attname) IS NOT NULL
    """
    database = cfg.get("database") or cfg.get("db")
    _cols, rows, _n = await db_svc._server_execute(cfg, database, sql)
    for _schema, table, col in rows:
        qtable = db_svc._quote_postgres(str(table))
        qcol = db_svc._quote_postgres(str(col))
        await db_svc._server_execute(
            cfg,
            database,
            f"SELECT setval(pg_get_serial_sequence(%s, %s), COALESCE((SELECT MAX({qcol}) FROM {qtable}), 1), true)",
            [str(table), str(col)],
        )


def _cfg_from_snap(snap: dict, *, prefer_root: bool = False) -> dict:
    engine = snap.get("engine")
    if engine == "sqlite":
        return {"engine": "sqlite", "path": snap.get("path") or ""}
    user = snap.get("user") or ""
    password = snap.get("password") or ""
    if prefer_root and (snap.get("root_password") or snap.get("root_user")):
        user = snap.get("root_user") or ("root" if engine == "mysql" else user)
        password = snap.get("root_password") or password
    cfg = {
        "id": "adopt",
        "engine": engine,
        "host": snap.get("host") or snap.get("container") or "",
        "port": int(snap.get("port") or (3306 if engine == "mysql" else 5432)),
        "user": user,
        "password": password,
        "database": snap.get("database") or "",
        "db": snap.get("database") or "",
    }
    return cfg


def _sqlite_open(cfg: dict):
    path = cfg.get("path") or ""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


async def _sqlite_dest_cfg(dest: dict) -> dict:
    container = dest.get("container") or ""
    remote = dest.get("path") or DEFAULT_CONTAINER_SQLITE
    tmp = Path("/tmp") / f"panel-adopt-{os.getpid()}.sqlite"
    try:
        await _sqlite_export_from_container(container, remote, tmp, lambda _t: None)
    except Exception:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(str(tmp)).close()
    return {"engine": "sqlite", "path": str(tmp)}


async def _push_sqlite_dest(dest: dict, local_path: str) -> None:
    if not local_path or not Path(local_path).exists():
        return
    container = dest.get("container") or ""
    remote = dest.get("path") or DEFAULT_CONTAINER_SQLITE
    await _restore_sqlite_into_container(container, remote, Path(local_path), lambda _t: None)


def _container_exists(name: str) -> bool:
    if not name:
        return False
    try:
        docker_svc.get_client().containers.get(name)
        return True
    except Exception:
        return False


def _mount_is_host_bind(container: str, dest: str, host_dir: Path) -> bool:
    try:
        info = docker_svc.container_inspect(container)
    except Exception:
        return False
    try:
        want = str(host_dir.resolve())
    except OSError:
        want = str(host_dir)
    dest_norm = dest.rstrip("/")
    for m in info.get("mounts") or []:
        if (m.get("destination") or "").rstrip("/") != dest_norm:
            continue
        if (m.get("type") or "").lower() != "bind":
            continue
        src = m.get("source") or ""
        try:
            if str(Path(src).resolve()) == want:
                return True
        except OSError:
            if src == want:
                return True
    return False


async def _docker_cli(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")


async def _docker_exec(container: str, args: list[str]) -> tuple[int, str]:
    return await _docker_cli("exec", container, *args)


async def _dump_sqlite(snap: dict, dump_path: Path, log: LogFn) -> None:
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = str(snap.get("path") or "").strip()
    container = snap.get("container") or ""
    host = Path(raw_path) if raw_path and not is_container_sqlite_path(raw_path) else None
    if host and host.is_file():
        log(f"Backing up host SQLite {host}\n")
        sqlite_consistent_copy(host, dump_path)
        return
    remote = raw_path if is_container_sqlite_path(raw_path) else DEFAULT_CONTAINER_SQLITE
    if container and _container_exists(container):
        await _sqlite_export_from_container(container, remote, dump_path, log)
        return
    raise RuntimeError("SQLite database file was not found on disk or in a container")


async def _sqlite_export_from_container(container: str, remote: str, dest: Path, log: LogFn) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_remote = "/tmp/panel-sqlite-export.sqlite"
    if _container_running(container):
        code, out = await _docker_exec(container, ["sqlite3", remote, f".backup {tmp_remote}"])
        if code == 0:
            log("Exported SQLite with sqlite3 .backup (includes WAL).\n")
            await _docker_cp(f"{container}:{tmp_remote}", str(dest))
            await _docker_exec(container, ["rm", "-f", tmp_remote])
            if not dest.is_file() or dest.stat().st_size == 0:
                raise RuntimeError("SQLite backup was empty")
            return
        log(f"sqlite3 .backup unavailable ({(out or str(code)).strip()}); copying db+wal instead.\n")
    await _cp_sqlite_family(container, remote, dest)
    log(f"Copied SQLite family from {container}:{remote}\n")


async def _cp_sqlite_family(container: str, remote: str, dest: Path) -> None:
    raw = dest.with_suffix(dest.suffix + ".raw")
    await _docker_cp(f"{container}:{remote}", str(raw))
    for extra in ("-wal", "-shm"):
        try:
            await _docker_cp(f"{container}:{remote}{extra}", str(raw) + extra)
        except RuntimeError:
            pass
    sqlite_consistent_copy(raw, dest)
    raw.unlink(missing_ok=True)
    Path(str(raw) + "-wal").unlink(missing_ok=True)
    Path(str(raw) + "-shm").unlink(missing_ok=True)


async def _restore_sqlite_into_container(container: str, remote: str, src: Path, log: LogFn) -> None:
    if not container:
        raise RuntimeError("No app container for SQLite restore")
    writers = [name for name in sqlite_writer_names(container) if _container_exists(name)]
    running = [name for name in writers if _container_running(name)]
    for name in running:
        log(f"Stopping {name} so SQLite can be replaced without corrupting WAL.\n")
        code, out = await _docker_cli("stop", name)
        if code != 0:
            raise RuntimeError(out or f"docker stop {name} failed")
    try:
        await _clear_sqlite_sidecars(container, remote, log)
        log(f"Copying SQLite file into {container}:{remote}\n")
        await _docker_cp(str(src), f"{container}:{remote}")
    finally:
        for name in running:
            log(f"Starting {name} again.\n")
            code, out = await _docker_cli("start", name)
            if code != 0:
                log(f"Could not start {name}: {out}\n")


async def _clear_sqlite_sidecars(container: str, remote: str, log: LogFn) -> None:
    code, out = await _docker_cli(
        "run",
        "--rm",
        "--volumes-from",
        container,
        "alpine",
        "rm",
        "-f",
        f"{remote}-wal",
        f"{remote}-shm",
    )
    if code != 0:
        log(f"Could not remove SQLite WAL/SHM before restore ({out.strip()}); continuing.\n")


def _previous_app_container(app: dict, new_container: str) -> str:
    from app.services import discover_svc

    ctx = discover_svc.app_database_context(app)
    name = _app_container_name(ctx.get("services") or {})
    if name and name != new_container and _container_exists(name):
        return name
    return ""


async def _container_path_has_files(container: str, path: str) -> bool:
    if _container_running(container):
        code, out = await _docker_exec(
            container,
            ["sh", "-c", f"find {path} -type f ! -name .gitignore 2>/dev/null | head -1"],
        )
        return code == 0 and bool(out.strip())
    code, out = await _docker_cli(
        "run",
        "--rm",
        "--volumes-from",
        container,
        "alpine",
        "sh",
        "-c",
        f"find {path} -type f ! -name .gitignore 2>/dev/null | head -1",
    )
    return code == 0 and bool(out.strip())


async def _copy_old_container_storage(app: dict, log: LogFn) -> int:
    old = _previous_app_container(app, f"{app['id']}_app")
    if not old:
        return 0
    path = Path(app["path"])
    copied = 0
    for src in OLD_STORAGE_SOURCES:
        if not await _container_path_has_files(old, src):
            continue
        host_dir = storage_host_dir_for_source(path, src)
        if _mount_is_host_bind(old, src, host_dir):
            continue
        host_dir.mkdir(parents=True, exist_ok=True)
        log(f"Copying {old}:{src} → {host_dir} (old Docker volume onto host)\n")
        try:
            await _docker_cp(f"{old}:{src}/.", str(host_dir))
            copied += 1
        except RuntimeError as e:
            log(f"Could not copy {old}:{src} ({e})\n")
    return copied


async def _docker_cp(src: str, dest: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "cp",
        src,
        dest,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace") or f"docker cp failed: {src} → {dest}")


async def _run_to_file(cmd: list[str], dest: Path) -> None:
    with dest.open("wb") as fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=fh,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace") or f"command failed with {proc.returncode}")
    if dest.stat().st_size == 0:
        raise RuntimeError("dump was empty")
