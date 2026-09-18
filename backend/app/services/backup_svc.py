from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.services import db_svc, deploy_svc, rclone_svc
from app.utils import read_json, write_json

scheduler: AsyncIOScheduler | None = None

FOLDER_LABELS = {
    "storage/app": "App files (uploads, attachments)",
    "storage/app/public": "Public files",
    "storage/app/private": "Private files",
    "storage/app/public/attachments": "Public attachments",
    "storage/app/private/attachments": "Private attachments",
    "storage/app/public/thumbnails": "Public thumbnails",
    "storage/app/public/uploads": "Public uploaded files",
    "public/uploads": "Public uploads",
    "public/storage": "Public storage",
    "uploads": "Uploads folder",
}
# Nothing is pre-checked. Whole storage/app is too broad (public + private).
DEFAULT_FILE_FOLDERS: set[str] = set()
SCAN_FOLDERS = [
    "storage/app",
    "storage/app/public",
    "storage/app/private",
    "public/uploads",
    "public/storage",
    "uploads",
]
SKIP_CHILD_DIRS = {
    "framework",
    "cache",
    "logs",
    "sessions",
    "views",
    "tmp",
    "temp",
    "livewire-tmp",
    "debugbar",
    "clockwork",
    "vendor",
    "node_modules",
    "bootstrap",
}


POPULAR_TIMEZONES = [
    "Europe/Zagreb",
    "Europe/Belgrade",
    "Europe/Sarajevo",
    "Europe/Ljubljana",
    "Europe/Podgorica",
    "Europe/Skopje",
    "Europe/Vienna",
    "Europe/Budapest",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Rome",
    "Europe/Zurich",
    "Europe/Amsterdam",
    "Europe/Brussels",
    "Europe/Prague",
    "Europe/Warsaw",
    "Europe/Athens",
    "Europe/Bucharest",
    "Europe/Sofia",
    "Europe/Istanbul",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Lisbon",
    "Europe/Madrid",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
]

CITY_LABELS = {
    "Europe/Zagreb": "Zagreb, Croatia",
    "Europe/Belgrade": "Belgrade, Serbia",
    "Europe/Sarajevo": "Sarajevo, Bosnia",
    "Europe/Ljubljana": "Ljubljana, Slovenia",
    "Europe/Podgorica": "Podgorica, Montenegro",
    "Europe/Skopje": "Skopje, North Macedonia",
    "Europe/Vienna": "Vienna, Austria",
    "Europe/Budapest": "Budapest, Hungary",
    "Europe/Berlin": "Berlin, Germany",
    "Europe/Paris": "Paris, France",
    "Europe/Rome": "Rome, Italy",
    "Europe/Zurich": "Zurich, Switzerland",
    "Europe/Amsterdam": "Amsterdam, Netherlands",
    "Europe/Brussels": "Brussels, Belgium",
    "Europe/Prague": "Prague, Czechia",
    "Europe/Warsaw": "Warsaw, Poland",
    "Europe/Athens": "Athens, Greece",
    "Europe/Bucharest": "Bucharest, Romania",
    "Europe/Sofia": "Sofia, Bulgaria",
    "Europe/Istanbul": "Istanbul, Türkiye",
    "Europe/London": "London, UK",
    "Europe/Dublin": "Dublin, Ireland",
    "Europe/Lisbon": "Lisbon, Portugal",
    "Europe/Madrid": "Madrid, Spain",
    "UTC": "UTC",
    "America/New_York": "New York",
    "America/Chicago": "Chicago",
    "America/Denver": "Denver",
    "America/Los_Angeles": "Los Angeles",
}


def _offset_label(zone: ZoneInfo) -> str:
    now = datetime.now(zone)
    delta = now.utcoffset()
    seconds = int(delta.total_seconds()) if delta else 0
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or get_panel_timezone())
    except Exception:
        return ZoneInfo("UTC")


def get_panel_timezone() -> str:
    settings = get_settings()
    cfg = read_json(settings.config_path, {}) or {}
    if isinstance(cfg, dict) and cfg.get("backup_timezone"):
        return str(cfg["backup_timezone"])
    return settings.tz or "UTC"


def set_panel_timezone(name: str) -> str:
    zone = _zone(name)
    iana = str(zone)
    settings = get_settings()
    cfg = read_json(settings.config_path, {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["backup_timezone"] = iana
    write_json(settings.config_path, cfg)
    return iana


def timezone_info(name: str | None = None) -> dict[str, str]:
    zone = _zone(name)
    now = datetime.now(zone)
    iana = str(zone)
    city = CITY_LABELS.get(iana) or iana.split("/")[-1].replace("_", " ")
    return {
        "iana": iana,
        "label": city,
        "abbrev": now.tzname() or "",
        "offset": _offset_label(zone),
    }


def list_timezones() -> list[dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    skip_prefixes = ("SystemV/", "posix/", "right/")
    names = []
    for name in available_timezones():
        if name.startswith(skip_prefixes) or name in {"Factory", "localtime"}:
            continue
        if name.startswith("Etc/") and name != "UTC":
            continue
        names.append(name)
    names = sorted(set(names) | set(POPULAR_TIMEZONES))
    popular = []
    rest = []
    for name in names:
        try:
            zone = ZoneInfo(name)
        except Exception:
            continue
        local = now_utc.astimezone(zone)
        city = CITY_LABELS.get(name) or name.split("/")[-1].replace("_", " ")
        item = {
            "iana": name,
            "label": city,
            "abbrev": local.tzname() or "",
            "offset": _offset_label(zone),
            "popular": name in POPULAR_TIMEZONES,
        }
        if item["popular"]:
            popular.append(item)
        else:
            rest.append(item)
    popular.sort(key=lambda z: POPULAR_TIMEZONES.index(z["iana"]) if z["iana"] in POPULAR_TIMEZONES else 99)
    return popular + rest


def backup_zone(job: dict | None = None) -> ZoneInfo:
    iana = None
    if job:
        iana = (job.get("schedule") or {}).get("timezone")
    return _zone(iana)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "backup"


GENERIC_DB_STEMS = {"database", "db", "data", "sqlite", "mysql", "postgres", "pgsql", "main"}


def _backup_file_stem(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-._")
    return s or "backup"


def _dated_backup_filename(stem: str, ext: str, job: dict | None = None) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    stamp = datetime.now(backup_zone(job)).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{_backup_file_stem(stem)}_{stamp}{ext}"


def compile_cron(schedule: dict[str, Any] | None) -> str:
    schedule = schedule or {}
    raw_time = str(schedule.get("time") or "03:00")
    parts = raw_time.split(":")
    hour = int(parts[0] or 3)
    minute = int(parts[1] or 0) if len(parts) > 1 else 0
    repeat = schedule.get("repeat") or "daily"
    if repeat == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if repeat == "weekly":
        dow = int(schedule.get("weekday") if schedule.get("weekday") is not None else 0)
        return f"{minute} {hour} * * {dow}"
    if repeat == "monthly":
        day = int(schedule.get("monthday") or 1)
        day = min(max(day, 1), 28)
        return f"{minute} {hour} {day} * *"
    return f"{minute} {hour} * * *"


def parse_cron(cron: str | None) -> dict[str, Any]:
    fallback = {
        "repeat": "daily",
        "time": "03:00",
        "weekday": 0,
        "monthday": 1,
        "timezone": get_panel_timezone(),
    }
    if not cron:
        return fallback
    parts = cron.split()
    if len(parts) != 5:
        return fallback
    minute, hour, dom, _month, dow = parts
    try:
        time = f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return fallback
    base = {**fallback, "time": time}
    if dom == "*" and dow == "*":
        return {**base, "repeat": "daily"}
    if dom == "*" and dow == "1-5":
        return {**base, "repeat": "weekdays"}
    if dom == "*" and dow.isdigit():
        return {**base, "repeat": "weekly", "weekday": int(dow)}
    if dom.isdigit() and dow == "*":
        return {**base, "repeat": "monthly", "monthday": int(dom)}
    return {**base, "repeat": "daily"}


def describe_schedule(job: dict) -> str:
    schedule = job.get("schedule") or parse_cron(job.get("cron"))
    time = schedule.get("time") or "03:00"
    repeat = schedule.get("repeat") or "daily"
    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    tz = timezone_info((schedule or {}).get("timezone"))
    suffix = f" ({tz['label']})"
    if repeat == "weekdays":
        return f"Weekdays at {time}{suffix}"
    if repeat == "weekly":
        day = days[int(schedule.get("weekday") or 0) % 7]
        return f"Every {day} at {time}{suffix}"
    if repeat == "monthly":
        return f"Monthly on day {int(schedule.get('monthday') or 1)} at {time}{suffix}"
    return f"Every day at {time}{suffix}"


def normalize_rel_path(rel: str) -> str:
    text = str(rel or "").replace("\\", "/").strip()
    if not text or text in (".", "./"):
        return ""
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Folder path cannot contain '..'.")
        parts.append(part)
    return "/".join(parts)


def resolve_app_path(root: Path, rel: str) -> Path:
    root_r = root.resolve()
    rel_n = normalize_rel_path(rel)
    src = (root_r / rel_n).resolve() if rel_n else root_r
    if src != root_r and root_r not in src.parents:
        raise ValueError(f"Invalid folder: {rel}")
    return src


def normalize_app_file_paths(paths: list | None, extra: str | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    raw_items = [*(paths or [])]
    if extra:
        raw_items.append(extra)
    for raw in raw_items:
        rel = normalize_rel_path(str(raw))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def discover_app_folders(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        try:
            rel_n = normalize_rel_path(rel)
        except ValueError:
            return
        if not rel_n or rel_n in seen:
            return
        target = root / rel_n
        if not target.is_dir():
            return
        seen.add(rel_n)
        found.append(
            {
                "path": rel_n,
                "label": FOLDER_LABELS.get(rel_n, rel_n),
                "selected": rel_n in DEFAULT_FILE_FOLDERS,
            }
        )

    for rel in SCAN_FOLDERS:
        target = root / rel
        if not target.is_dir():
            continue
        add(rel)
        try:
            children = sorted(target.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name.lower() in SKIP_CHILD_DIRS:
                continue
            add(f"{rel}/{child.name}")
    found.sort(key=lambda f: f["path"])
    return found


def list_app_source_dirs(app_id: str, rel: str = "") -> dict[str, Any]:
    app = deploy_svc.get_app(app_id)
    if not app:
        raise ValueError("Unknown app")
    root = Path(app.get("path") or "")
    if not root.is_dir():
        raise FileNotFoundError("App folder not found")
    rel_n = normalize_rel_path(rel)
    current = resolve_app_path(root, rel_n)
    if not current.is_dir():
        raise FileNotFoundError(f"Folder not found: {rel_n or '/'}")
    dirs = []
    try:
        children = sorted(current.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        raise FileNotFoundError(f"Cannot read folder: {rel_n or '/'}") from e
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        child_rel = f"{rel_n}/{child.name}" if rel_n else child.name
        dirs.append({"name": child.name, "path": child_rel})
    parent = "/".join(rel_n.split("/")[:-1]) if rel_n else None
    return {"path": rel_n, "parent": parent, "dirs": dirs}


def list_backup_targets() -> dict[str, Any]:
    apps_out = []
    for app in deploy_svc.list_apps():
        root = Path(app.get("path") or "")
        folders = discover_app_folders(root) if root.is_dir() else []
        apps_out.append(
            {
                "id": app["id"],
                "name": app.get("name") or app["id"],
                "path": app.get("path") or "",
                "folders": folders,
            }
        )
    remotes: list[str] = []
    current = timezone_info()
    return {
        "apps": apps_out,
        "databases": db_svc.list_connections(),
        "remotes": remotes,
        "timezone": current,
        "timezones": list_timezones(),
    }


def list_backup_jobs() -> list[dict]:
    settings = get_settings()
    return read_json(settings.backups_path, [])


def save_backup_jobs(jobs: list[dict]) -> None:
    settings = get_settings()
    write_json(settings.backups_path, jobs)


def get_backup_job(job_id: str) -> dict | None:
    for j in list_backup_jobs():
        if j.get("id") == job_id:
            return j
    return None


def normalize_job_notify(notify: dict | None, existing: dict | None = None) -> dict:
    notify = notify or {}
    existing = existing or {}
    account_id = (notify.get("account_id") if "account_id" in notify else existing.get("account_id") or "").strip()
    on = (notify.get("on") if "on" in notify else existing.get("on") or "always").strip().lower()
    if on not in ("always", "failure", "success"):
        on = "always"
    group_id = (notify.get("group_id") if "group_id" in notify else existing.get("group_id") or "").strip()

    contact_ids = notify.get("recipient_ids") if "recipient_ids" in notify else notify.get("contact_ids")
    if contact_ids is None:
        contact_ids = existing.get("recipient_ids") or existing.get("contact_ids") or []
    if not isinstance(contact_ids, list):
        contact_ids = []
    contact_ids = [str(c).strip() for c in contact_ids if str(c).strip()]

    to_emails_raw = notify.get("to_emails") if "to_emails" in notify else existing.get("to_emails") or []
    if not isinstance(to_emails_raw, list):
        to_emails_raw = []
    from app.services.mail_svc import _valid_email

    to_emails = [e.strip() for e in to_emails_raw if isinstance(e, str) and e.strip() and _valid_email(e.strip())]

    in_group = bool(group_id)
    enabled = bool(notify.get("enabled")) and bool(account_id) and not in_group

    return {
        "enabled": enabled,
        "account_id": account_id,
        "group_id": group_id,
        "recipient_ids": contact_ids,
        "to_emails": to_emails,
        "on": on,
        "subject_success": (notify.get("subject_success") or existing.get("subject_success") or "").strip(),
        "title_success": (notify.get("title_success") or existing.get("title_success") or "").strip(),
        "message_success": (notify.get("message_success") or existing.get("message_success") or "").strip(),
        "subject_failure": (notify.get("subject_failure") or existing.get("subject_failure") or "").strip(),
        "title_failure": (notify.get("title_failure") or existing.get("title_failure") or "").strip(),
        "message_failure": (notify.get("message_failure") or existing.get("message_failure") or "").strip(),
    }


def upsert_backup_job(job: dict) -> dict:
    if job.get("schedule"):
        tz_name = (job["schedule"].get("timezone") or "").strip()
        if tz_name:
            job["schedule"]["timezone"] = set_panel_timezone(tz_name)
        else:
            job["schedule"]["timezone"] = get_panel_timezone()
        job["cron"] = compile_cron(job["schedule"])
    elif job.get("cron") and not job.get("schedule"):
        job["schedule"] = parse_cron(job["cron"])
        job["schedule"]["timezone"] = get_panel_timezone()
    remote = (job.get("destination_remote") or "").strip()
    folder = (job.get("destination_folder") or "").strip().strip("/")
    if remote:
        job["destination"] = f"{remote}:{folder}" if folder else f"{remote}:"
    source = job.get("source") or {}
    stype = source.get("type")
    if stype == "app_files":
        rels = normalize_app_file_paths(source.get("paths"), source.get("path"))
        if not rels:
            raise ValueError("Choose at least one folder to sync.")
        source = {**source, "paths": rels}
        source.pop("path", None)
        job["source"] = source
    if not job.get("kind"):
        job["kind"] = "files" if stype in ("app_files", "path") else "database"
    jobs = list_backup_jobs()
    if not job.get("id"):
        job["id"] = str(uuid.uuid4())[:8]
    existing_notify = None
    for existing in jobs:
        if existing.get("id") == job.get("id"):
            existing_notify = existing.get("notify")
            break
    job["notify"] = normalize_job_notify(job.get("notify"), existing_notify)
    replaced = False
    for i, existing in enumerate(jobs):
        if existing["id"] == job["id"]:
            jobs[i] = {**existing, **job}
            job = jobs[i]
            replaced = True
            break
    if not replaced:
        jobs.append(job)
    save_backup_jobs(jobs)
    reschedule()
    return job


def clear_notify_for_account(account_id: str) -> None:
    jobs = list_backup_jobs()
    changed = False
    for job in jobs:
        notify = job.get("notify") or {}
        if notify.get("account_id") == account_id:
            job["notify"] = normalize_job_notify(
                {
                    "enabled": False,
                    "account_id": "",
                    "on": notify.get("on") or "always",
                    "subject_success": notify.get("subject_success") or "",
                    "title_success": notify.get("title_success") or "",
                    "message_success": notify.get("message_success") or "",
                    "subject_failure": notify.get("subject_failure") or "",
                    "title_failure": notify.get("title_failure") or "",
                    "message_failure": notify.get("message_failure") or "",
                    "recipient_ids": notify.get("recipient_ids") or notify.get("contact_ids") or [],
                    "to_emails": notify.get("to_emails") or [],
                    "group_id": notify.get("group_id") or "",
                }
            )
            changed = True
    if changed:
        save_backup_jobs(jobs)
    from app.services import notify_groups_svc

    notify_groups_svc.clear_notify_for_account(account_id)


def remove_recipient_from_jobs(recipient_id: str) -> None:
    jobs = list_backup_jobs()
    changed = False
    for job in jobs:
        notify = job.get("notify") or {}
        ids = notify.get("recipient_ids") or notify.get("contact_ids") or []
        if recipient_id in ids:
            key = "recipient_ids" if "recipient_ids" in notify else "contact_ids"
            notify[key] = [i for i in ids if i != recipient_id]
            job["notify"] = notify
            changed = True
    if changed:
        save_backup_jobs(jobs)


def remove_contact_from_jobs(contact_id: str) -> None:
    remove_recipient_from_jobs(contact_id)


def delete_backup_job(job_id: str) -> bool:
    jobs = list_backup_jobs()
    new_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        return False
    save_backup_jobs(new_jobs)
    from app.services import notify_groups_svc

    notify_groups_svc.remove_job_from_groups(job_id)
    reschedule()
    return True


def list_history(limit: int = 50) -> list[dict]:
    settings = get_settings()
    hist = read_json(settings.backup_history_path, [])
    return hist[:limit]


def _append_history(entry: dict) -> None:
    settings = get_settings()
    hist = read_json(settings.backup_history_path, [])
    hist.insert(0, entry)
    write_json(settings.backup_history_path, hist[:200])


def _sync_stats_log_line(stats: dict[str, int | float]) -> str:
    return (
        f"Sync summary: {int(stats.get('transfers', 0))} uploaded/updated, "
        f"{int(stats.get('checks', 0))} unchanged, "
        f"{int(stats.get('deletes', 0))} removed, "
        f"{int(stats.get('bytes', 0))} bytes"
    )


async def run_backup_job(job_id: str, *, manual: bool = False) -> dict:
    job = get_backup_job(job_id)
    if not job:
        raise ValueError(f"Unknown backup job: {job_id}")

    settings = get_settings()
    run_id = str(uuid.uuid4())[:8]
    started = datetime.now(timezone.utc).isoformat()
    log_parts: list[str] = []
    staging = Path(settings.data_dir) / "backup_staging" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    status = "success"
    sync_stats: dict[str, int | float] | None = None
    try:
        source = job.get("source", {})
        stype = source.get("type")
        local_path: Path | None = None

        if stype in ("mysql", "postgres"):
            conn_id = source.get("connection_id")
            engine = "postgres" if stype == "postgres" else "mysql"
            if not conn_id:
                raise ValueError(f"source.connection_id required for {engine} backups")
            cfg = db_svc.get_db_config(conn_id)
            db_name = (
                (source.get("database") or "").strip()
                or cfg.get("database")
                or cfg.get("db")
                or cfg.get("name")
                or job.get("name")
                or engine
            )
            dump = await db_svc.dump_database(conn_id, source.get("database"))
            local_path = staging / _dated_backup_filename(db_name, ".sql", job)
            local_path.write_bytes(dump)
            label = "PostgreSQL" if engine == "postgres" else "MySQL"
            log_parts.append(f"Dumped {label} to {local_path.name} ({len(dump)} bytes)")
        elif stype == "sqlite":
            conn_id = source.get("connection_id")
            cfg = db_svc.get_db_config(conn_id) if conn_id else {}
            if conn_id:
                src = Path(cfg.get("path") or source.get("path", ""))
            else:
                src = Path(source.get("path", ""))
            if not src.exists():
                raise FileNotFoundError(f"SQLite path not found: {src}")
            stem = src.stem
            if stem.lower() in GENERIC_DB_STEMS:
                stem = cfg.get("name") or job.get("name") or src.parent.name or "sqlite"
            local_path = staging / _dated_backup_filename(stem, src.suffix or ".sqlite", job)
            shutil.copy2(src, local_path)
            log_parts.append(f"Copied SQLite {src} -> {local_path.name}")
        elif stype in ("path", "app_files"):
            dest = job.get("destination", "")
            if not dest:
                raise ValueError("destination required (pick a storage location)")
            if stype == "app_files":
                app = deploy_svc.get_app(source.get("app_id") or "")
                if not app:
                    raise ValueError("Choose an app to back up.")
                root = Path(app.get("path") or "")
                rels = normalize_app_file_paths(source.get("paths"), source.get("path"))
                if not rels:
                    raise ValueError("Choose at least one folder to sync.")
                if not root.is_dir():
                    raise FileNotFoundError(f"App folder not found: {root}")
                stat_parts: list[dict[str, int | float]] = []
                for idx, rel in enumerate(rels):
                    src = resolve_app_path(root, rel)
                    if not src.exists():
                        raise FileNotFoundError(f"Folder not found: {src}")
                    # Contents of the chosen folder go into dest (no storage/app wrapping).
                    log_parts.append(f"Sync {src} -> {dest}")
                    group = f"panel-backup-{run_id}-{idx}"
                    part = await rclone_svc.sync_and_stats(
                        str(src),
                        dest,
                        group=group,
                        create_empty_src_dirs=True,
                    )
                    stat_parts.append(part)
                sync_stats = rclone_svc.merge_sync_stats(stat_parts)
                log_parts.append(_sync_stats_log_line(sync_stats))
                local_path = None
            else:
                src = Path(source.get("path", ""))
                if not src.exists():
                    raise FileNotFoundError(f"Source path not found: {src}")
                log_parts.append(f"Sync {src} -> {dest}")
                group = f"panel-backup-{run_id}"
                sync_stats = await rclone_svc.sync_and_stats(
                    str(src),
                    dest,
                    group=group,
                    create_empty_src_dirs=True,
                )
                log_parts.append(_sync_stats_log_line(sync_stats))
                local_path = None
        else:
            raise ValueError(f"Unknown source type: {stype}")

        dest = job.get("destination", "")
        if not dest:
            raise ValueError("destination required (pick a storage location)")

        if local_path is not None:
            src_fs = f"/data/backup_staging/{run_id}"
            result = await rclone_svc.rc_call(
                "sync/copy",
                {
                    "srcFs": src_fs,
                    "dstFs": dest,
                    "_async": False,
                },
            )
            log_parts.append(f"rclone copy {local_path.name} -> {dest}: {result}")
    except Exception as e:
        status = "failed"
        log_parts.append(f"ERROR: {e}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    entry: dict[str, Any] = {
        "id": run_id,
        "job_id": job_id,
        "job_name": job.get("name"),
        "status": status,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "log": "\n".join(log_parts),
    }
    if sync_stats is not None:
        entry["sync_stats"] = sync_stats

    from app.services import mail_svc, notify_groups_svc

    group_id = ((job.get("notify") or {}).get("group_id") or "").strip()
    if group_id:
        try:
            group_msg = await notify_groups_svc.record_group_run(job, entry)
            if group_msg:
                log_parts.append(group_msg)
        except Exception as e:
            log_parts.append(f"Could not send group email: {e}")
        if manual:
            try:
                if await notify_groups_svc.notify_manual_job(job, entry):
                    log_parts.append("Status email sent (manual run).")
            except Exception as e:
                log_parts.append(f"Could not send email: {e}")
        entry["log"] = "\n".join(log_parts)
    elif mail_svc.should_notify(job, status):
        try:
            await mail_svc.notify_backup(job, entry)
            log_parts.append("Status email sent.")
        except Exception as e:
            log_parts.append(f"Could not send email: {e}")
        entry["log"] = "\n".join(log_parts)

    _append_history(entry)

    # update last_run on job
    jobs = list_backup_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["last_run"] = entry
            break
    save_backup_jobs(jobs)
    return entry


def reschedule() -> None:
    global scheduler
    if scheduler is None:
        return
    scheduler.remove_all_jobs()
    for job in list_backup_jobs():
        cron = job.get("cron")
        if not cron or not job.get("enabled", True):
            continue
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=backup_zone(job))
            scheduler.add_job(
                run_backup_job,
                trigger=trigger,
                args=[job["id"]],
                id=f"backup-{job['id']}",
                replace_existing=True,
            )
        except Exception:
            continue
    _schedule_cert_renewal()


async def _renew_certificates_job() -> None:
    from app.services import certs_svc

    try:
        await certs_svc.renew_certificates()
    except Exception:
        pass


def _schedule_cert_renewal() -> None:
    if scheduler is None:
        return
    try:
        scheduler.add_job(
            _renew_certificates_job,
            CronTrigger(hour="3,15", minute=10, timezone=backup_zone()),
            id="certs-renew",
            replace_existing=True,
        )
    except Exception:
        pass


def repoint_connection_ids(mapping: dict[str, str]) -> int:
    """Rewrite backup job database connections after a host/container rename."""
    mapping = {old: new for old, new in (mapping or {}).items() if old and new and old != new}
    if not mapping:
        return 0
    jobs = list_backup_jobs()
    changed = 0
    for job in jobs:
        source = job.get("source") or {}
        old = source.get("connection_id")
        new = mapping.get(old)
        if not new:
            continue
        source["connection_id"] = new
        job["source"] = source
        changed += 1
    if changed:
        save_backup_jobs(jobs)
    return changed


def start_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        return
    try:
        from app.services import laravel_svc

        laravel_svc.repoint_managed_database_targets()
    except Exception:
        pass
    scheduler = AsyncIOScheduler(timezone=backup_zone())
    scheduler.start()
    reschedule()
    from app.services import notify_groups_svc

    notify_groups_svc.restore_pending_timeouts()


def stop_scheduler() -> None:
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
