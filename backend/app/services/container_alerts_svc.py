from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.services import backup_svc, docker_svc, mail_svc
from app.services.log_match import log_line_is_error
from app.utils import read_json, write_json

logger = logging.getLogger(__name__)

JOB_ID = "container-alerts"
POLL_SECONDS = 30
DEFAULT_COOLDOWN_MINUTES = 15
# A single Docker restart is not a loop. Queue workers with --max-time=3600
# exit cleanly every hour; Docker then restarts them. See docs/AUDIT.md.
RESTART_BURST_WINDOW = timedelta(minutes=10)
RESTART_BURST_COUNT = 3
RESTARTING_STREAK_FOR_LOOP = 2
# 0 = clean, 143 = SIGTERM (docker stop / --max-time), 137 = SIGKILL
# (compose recreate / stop-timeout). Not OOM unless Docker sets OOMKilled.
CLEAN_EXIT_CODES = {0, 143, 137}
_SKIP_LOG_SCAN_NAMES = {"vps_dashboard"}
_HASH_CHECKPOINT_RE = re.compile(r"^[0-9a-f]{64}$")
_RECYCLER_SERVICES = {"queue", "horizon", "worker", "jobs"}
_RECYCLER_NAME_RE = re.compile(r"(?:^|[._-])(queue|horizon|worker)s?$", re.I)
_RECYCLER_CMD_RE = re.compile(
    r"queue:work|queue:listen|horizon:work|horizon:listen|"
    r"artisan horizon|(?:^|\s)--max-time\b|(?:^|\s)--max-jobs\b|"
    r"(?:^|\s)--max-tasks-per-child\b",
    re.I,
)

DEFAULT_EVENTS = {
    "crash": True,
    "restart_loop": True,
    "unhealthy": True,
    "oom": True,
    "restart_count": True,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "notify": {
        "account_id": "",
        "recipient_ids": [],
        "to_emails": [],
    },
    "events": dict(DEFAULT_EVENTS),
    "scan_logs": False,
    "cooldown_minutes": DEFAULT_COOLDOWN_MINUTES,
}


def _config_path():
    return get_settings().container_alerts_path


def _state_path():
    return get_settings().container_alert_state_path


def _normalize_notify(notify: dict | None, existing: dict | None = None) -> dict:
    notify = notify or {}
    existing = existing or {}
    account_id = (
        notify.get("account_id")
        if "account_id" in notify
        else existing.get("account_id") or ""
    ).strip()
    recipient_ids = notify.get("recipient_ids")
    if recipient_ids is None:
        recipient_ids = existing.get("recipient_ids") or []
    to_emails = notify.get("to_emails")
    if to_emails is None:
        to_emails = existing.get("to_emails") or []
    return {
        "account_id": account_id,
        "recipient_ids": [str(r).strip() for r in recipient_ids if str(r).strip()],
        "to_emails": [str(e).strip() for e in to_emails if str(e).strip()],
    }


def _normalize_events(events: dict | None, existing: dict | None = None) -> dict:
    events = events or {}
    existing = existing or {}
    out = {}
    for key, default in DEFAULT_EVENTS.items():
        if key in events:
            out[key] = bool(events[key])
        elif key in existing:
            out[key] = bool(existing[key])
        else:
            out[key] = default
    return out


def _normalize_config(raw: dict | None, existing: dict | None = None) -> dict:
    raw = raw or {}
    base = existing if existing is not None else DEFAULT_CONFIG
    notify = _normalize_notify(raw.get("notify"), base.get("notify"))
    events = _normalize_events(raw.get("events"), base.get("events"))
    cooldown = raw.get("cooldown_minutes", base.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
    try:
        cooldown = max(1, min(1440, int(cooldown)))
    except (TypeError, ValueError):
        cooldown = DEFAULT_COOLDOWN_MINUTES
    enabled = raw.get("enabled") if "enabled" in raw else base.get("enabled", False)
    scan_logs = raw.get("scan_logs") if "scan_logs" in raw else base.get("scan_logs", False)
    return {
        "enabled": bool(enabled),
        "notify": notify,
        "events": events,
        "scan_logs": bool(scan_logs),
        "cooldown_minutes": cooldown,
    }


def get_config() -> dict:
    data = read_json(_config_path(), None)
    if not isinstance(data, dict):
        return _normalize_config({})
    return _normalize_config(data)


def save_config(raw: dict) -> dict:
    current = read_json(_config_path(), None)
    existing = _normalize_config(current) if isinstance(current, dict) else None
    config = _normalize_config(raw, existing)
    write_json(_config_path(), config)
    reschedule_monitor()
    return config


def load_state() -> dict:
    data = read_json(_state_path(), {})
    if not isinstance(data, dict):
        return {"containers": {}, "last_check": None}
    containers = data.get("containers")
    if not isinstance(containers, dict):
        containers = {}
    return {"containers": containers, "last_check": data.get("last_check")}


def save_state(state: dict) -> None:
    write_json(_state_path(), state)


def _cmd_blob(container: dict) -> str:
    parts: list[str] = []
    for key in ("entrypoint", "cmd", "command"):
        val = container.get(key)
        if isinstance(val, list):
            parts.extend(str(x) for x in val if x is not None)
        elif val:
            parts.append(str(val))
    return " ".join(parts)


def is_expected_recycler(container: dict) -> bool:
    """True for workers that are supposed to exit and be restarted by Docker."""
    service = (container.get("compose_service") or "").strip().lower()
    if service in _RECYCLER_SERVICES:
        return True
    name = (container.get("name") or "").strip()
    if name and _RECYCLER_NAME_RE.search(name):
        return True
    return bool(_RECYCLER_CMD_RE.search(_cmd_blob(container)))


def _clean_exit(exit_code: Any) -> bool:
    try:
        return int(exit_code) in CLEAN_EXIT_CODES
    except (TypeError, ValueError):
        return False


def _is_restarting(status: str, issue_kind: str | None) -> bool:
    return status == "restarting" or issue_kind == "restarting"


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _prune_restart_history(history: list, now: datetime) -> list[str]:
    cutoff = now - RESTART_BURST_WINDOW
    out: list[str] = []
    for item in history or []:
        raw = item.isoformat() if isinstance(item, datetime) else str(item)
        dt = _parse_iso(raw)
        if dt is not None and dt >= cutoff:
            out.append(dt.isoformat())
    return out


def _is_planned_recycle(
    recycler: bool, exit_code: Any, delta: int, burst: int
) -> bool:
    """True for a single clean worker recycle or docker restart, not a crash burst."""
    if burst >= RESTART_BURST_COUNT or delta > 1:
        return False
    if _clean_exit(exit_code):
        return True
    return bool(recycler and exit_code is None)


def _snapshot(container: dict) -> dict:
    issue = container.get("issue") or {}
    return {
        "status": (container.get("status") or "").lower(),
        "state": (container.get("state") or "").lower(),
        "health": (container.get("health") or "").lower() or None,
        "restart_count": int(container.get("restart_count") or 0),
        "issue_kind": issue.get("kind"),
        "exit_code": container.get("exit_code"),
        "expected_recycler": is_expected_recycler(container),
    }


def _event_enabled(config: dict, event: str) -> bool:
    events = config.get("events") or {}
    if event == "log_error":
        return bool(config.get("scan_logs"))
    return bool(events.get(event))


def _cooldown_active(cooldowns: dict, event: str, now: datetime, minutes: int) -> bool:
    raw = cooldowns.get(event)
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now < until


def _set_cooldown(cooldowns: dict, event: str, now: datetime, minutes: int) -> None:
    cooldowns[event] = (now + timedelta(minutes=minutes)).isoformat()


def _detect_events(
    container: dict,
    prev: dict | None,
    config: dict,
    extra: dict | None = None,
    now: datetime | None = None,
) -> tuple[list[tuple[str, dict]], dict]:
    extra = extra or {}
    now = now or datetime.now(timezone.utc)
    snap = _snapshot(container)
    curr_restart = int(snap.get("restart_count") or 0)
    status = snap.get("status") or ""
    issue_kind = snap.get("issue_kind")
    currently_restarting = _is_restarting(status, issue_kind)

    if currently_restarting:
        streak = int(extra.get("restarting_streak") or 0) + 1
        last_exit = container.get("exit_code")
        if last_exit is None:
            last_exit = extra.get("last_exit_code")
    else:
        streak = 0
        last_exit = extra.get("last_exit_code")

    history = _prune_restart_history(extra.get("restart_history") or [], now)
    prev_restart = int((prev or {}).get("restart_count") or 0)
    if prev is not None and curr_restart < prev_restart:
        history = []
    delta = 0
    if prev is not None and curr_restart > prev_restart:
        delta = curr_restart - prev_restart
        history.extend([now.isoformat()] * delta)
        if container.get("exit_code") is not None and currently_restarting:
            last_exit = container.get("exit_code")

    next_extra = {
        "restarting_streak": streak,
        "restart_history": history[-20:],
        "last_exit_code": last_exit,
    }

    if prev is None:
        return [], next_extra

    events: list[tuple[str, dict]] = []
    issue = container.get("issue") or {}
    health = snap.get("health") or ""
    prev_health = (prev.get("health") or "").lower() or None
    prev_issue_kind = prev.get("issue_kind")
    recycler = is_expected_recycler(container)
    burst = len(history)
    prev_was_restarting = _is_restarting(
        (prev.get("status") or "").lower(),
        prev.get("issue_kind"),
    )
    exit_this_cycle = last_exit if (currently_restarting or prev_was_restarting) else None

    details = {
        "issue": issue,
        "previous": prev,
        "current": snap,
    }

    if _event_enabled(config, "oom") and issue_kind == "oom" and prev_issue_kind != "oom":
        events.append(("oom", {**details, "title": "Ran out of memory"}))

    if _event_enabled(config, "restart_loop"):
        stuck = currently_restarting and streak >= RESTARTING_STREAK_FOR_LOOP
        if burst >= RESTART_BURST_COUNT or stuck:
            events.append(("restart_loop", {**details, "title": "Keeps restarting"}))

    if _event_enabled(config, "crash"):
        crash_kinds = {"crashed", "dead"}
        if issue_kind in crash_kinds and prev_issue_kind not in crash_kinds:
            title = issue.get("title") or "Crashed"
            events.append(("crash", {**details, "title": title}))

    if _event_enabled(config, "unhealthy"):
        if health == "unhealthy" and prev_health != "unhealthy":
            events.append(
                (
                    "unhealthy",
                    {**details, "title": issue.get("title") or "Not responding"},
                )
            )

    if (
        _event_enabled(config, "restart_count")
        and not currently_restarting
    ):
        recovered_crash = (
            prev_was_restarting
            and exit_this_cycle is not None
            and not _clean_exit(exit_this_cycle)
        )
        if (delta > 0 or recovered_crash) and not _is_planned_recycle(
            recycler, exit_this_cycle, delta, burst
        ):
            events.append(
                (
                    "restart_count",
                    {
                        **details,
                        "title": "Container restarted",
                        "restart_delta": max(delta, 1),
                    },
                )
            )

    return events, next_extra


def skip_log_scan(container: dict) -> bool:
    """The panel must not scan its own logs — mail failures become more mail."""
    name = (container.get("name") or "").strip()
    return name in _SKIP_LOG_SCAN_NAMES


def _is_hash_checkpoint(raw: str | None) -> bool:
    return bool(raw and _HASH_CHECKPOINT_RE.fullmatch(raw.strip()))


def _docker_log_ts_raw(line: str) -> str | None:
    if not line:
        return None
    space = line.find(" ")
    if space < 10 or "T" not in line[:space]:
        return None
    return line[:space]


def _docker_log_ts(line: str) -> datetime | None:
    return _parse_iso(_docker_log_ts_raw(line))


def scan_log_text(raw: str, prev_checkpoint: str | None) -> tuple[list[str], str]:
    """Alert only on error lines newer than the last checkpoint timestamp.

    A hash of the whole tail window re-fires on old errors when any new line
    appears. First run and leftover sha256 checkpoints are silent bootstraps.
    """
    lines = raw.splitlines()
    last_raw = None
    for line in reversed(lines):
        last_raw = _docker_log_ts_raw(line)
        if last_raw:
            break
    new_checkpoint = last_raw or prev_checkpoint or ""

    if not prev_checkpoint or _is_hash_checkpoint(prev_checkpoint):
        return [], new_checkpoint

    prev_ts = _parse_iso(prev_checkpoint)
    if prev_ts is None:
        return [], new_checkpoint

    matches: list[str] = []
    for line in lines:
        ts = _docker_log_ts(line)
        if ts is None or ts <= prev_ts:
            continue
        if log_line_is_error(line):
            matches.append(line)
    return matches[-5:], new_checkpoint


def _scan_logs(container_name: str, prev_checkpoint: str | None) -> tuple[list[str], str]:
    try:
        raw = docker_svc.container_logs(container_name, tail=100)
    except Exception as exc:
        logger.warning("Log scan failed for %s: %s", container_name, exc)
        return [], prev_checkpoint or ""

    return scan_log_text(raw, prev_checkpoint)


def _can_notify(config: dict) -> bool:
    if not config.get("enabled"):
        return False
    notify = config.get("notify") or {}
    if not (notify.get("account_id") or "").strip():
        return False
    return bool(mail_svc.resolve_notify_recipients(notify))


async def _send_events(config: dict, container: dict, events: list[tuple[str, dict]]) -> None:
    for event_type, details in events:
        try:
            await mail_svc.notify_container_event(config, container, event_type, details)
        except Exception as exc:
            logger.exception(
                "Failed to send container alert for %s (%s): %s",
                container.get("name"),
                event_type,
                exc,
            )


def check_containers() -> None:
    config = get_config()
    state = load_state()
    now = datetime.now(timezone.utc)
    cooldown_minutes = int(config.get("cooldown_minutes") or DEFAULT_COOLDOWN_MINUTES)

    try:
        containers = docker_svc.list_containers(True)
    except Exception as exc:
        logger.warning("Container alert poll failed: %s", exc)
        return

    can_send = _can_notify(config)
    known = state.get("containers") or {}
    next_known: dict[str, dict] = {}
    pending: list[tuple[dict, list[tuple[str, dict]]]] = []

    for container in containers:
        name = container.get("name") or container.get("id") or ""
        if not name:
            continue

        prev_entry = known.get(name) or {}
        prev_snap = prev_entry.get("snapshot")
        cooldowns = dict(prev_entry.get("cooldowns") or {})
        log_checkpoint = prev_entry.get("log_checkpoint")
        extra = {
            "restarting_streak": prev_entry.get("restarting_streak") or 0,
            "restart_history": prev_entry.get("restart_history") or [],
            "last_exit_code": prev_entry.get("last_exit_code"),
        }

        detected, extra = _detect_events(
            container, prev_snap, config, extra=extra, now=now
        )

        if (
            config.get("scan_logs")
            and _event_enabled(config, "log_error")
            and not skip_log_scan(container)
        ):
            matches, new_checkpoint = _scan_logs(name, log_checkpoint)
            if matches and not _cooldown_active(cooldowns, "log_error", now, cooldown_minutes):
                detected.append(
                    (
                        "log_error",
                        {
                            "title": "Error in logs",
                            "log_lines": matches,
                            "current": _snapshot(container),
                        },
                    )
                )
            log_checkpoint = new_checkpoint
        elif not log_checkpoint:
            try:
                raw = docker_svc.container_logs(name, tail=50)
                log_checkpoint = hashlib.sha256(
                    raw.encode("utf-8", errors="replace")
                ).hexdigest()
            except Exception:
                log_checkpoint = prev_entry.get("log_checkpoint")

        filtered: list[tuple[str, dict]] = []
        for event_type, details in detected:
            if _cooldown_active(cooldowns, event_type, now, cooldown_minutes):
                continue
            filtered.append((event_type, details))
            _set_cooldown(cooldowns, event_type, now, cooldown_minutes)

        if filtered and can_send:
            pending.append((container, filtered))

        next_known[name] = {
            "snapshot": _snapshot(container),
            "cooldowns": cooldowns,
            "log_checkpoint": log_checkpoint,
            **extra,
        }

    state["containers"] = next_known
    state["last_check"] = now.isoformat()
    save_state(state)

    if pending:
        loop = asyncio.new_event_loop()
        try:
            for container, events in pending:
                loop.run_until_complete(_send_events(config, container, events))
        finally:
            loop.close()


async def send_test_alert() -> None:
    config = get_config()
    if not _can_notify(config):
        raise ValueError(
            "Enable alerts and choose a mail account with at least one recipient."
        )
    sample = {
        "name": "example_app",
        "image": "nginx:latest",
        "status": "exited",
        "health": None,
        "compose_project": "example",
        "issue": {
            "kind": "crashed",
            "severity": "error",
            "title": "Crashed",
            "summary": "The app quit unexpectedly.",
            "suggestion": "Try Restart. If it happens again, open Logs for details.",
        },
    }
    details = {
        "title": "Test alert",
        "issue": sample["issue"],
        "current": _snapshot(sample),
    }
    await mail_svc.notify_container_event(config, sample, "crash", details, test=True)


def reschedule_monitor() -> None:
    scheduler = backup_svc.scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job(JOB_ID)
    except Exception:
        pass
    config = get_config()
    if not config.get("enabled"):
        return
    try:
        scheduler.add_job(
            check_containers,
            IntervalTrigger(seconds=POLL_SECONDS),
            id=JOB_ID,
            replace_existing=True,
        )
    except Exception as exc:
        logger.warning("Failed to schedule container alerts: %s", exc)


def start_monitor() -> None:
    reschedule_monitor()
