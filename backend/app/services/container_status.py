from __future__ import annotations

import re
from typing import Any

MAX_OUTPUT = 240

_NEVER_FINISHED = "0001-01-01"


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def sanitize_output(text: str | None, max_len: int = MAX_OUTPUT) -> str:
    cleaned = _collapse(text or "")
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1] + "…"
    return cleaned


def _finished_at(value: str | None) -> str | None:
    if not value or value.startswith(_NEVER_FINISHED):
        return None
    return value


def last_health_entry(health: dict | None) -> dict | None:
    log = (health or {}).get("Log") or []
    if not log:
        return None
    return log[-1]


def health_check_history(attrs: dict, limit: int = 5) -> list[dict]:
    health = (attrs.get("State") or {}).get("Health") or {}
    log = health.get("Log") or []
    out = []
    for entry in log[-limit:]:
        out.append(
            {
                "start": entry.get("Start"),
                "end": entry.get("End"),
                "exit_code": entry.get("ExitCode"),
                "output": sanitize_output(entry.get("Output")),
            }
        )
    return out


def _facts(
    state: dict,
    *,
    restart_count: int = 0,
    health: dict | None = None,
    last_output: str | None = None,
) -> dict[str, Any]:
    health = health or {}
    last = last_health_entry(health)
    output = last_output
    if output is None and last:
        output = sanitize_output(last.get("Output"))
    error = _collapse(state.get("Error") or "") or None
    streak = health.get("FailingStreak")
    return {
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "failing_streak": streak if streak is not None else None,
        "finished_at": _finished_at(state.get("FinishedAt")),
        "restart_count": restart_count or 0,
        "error": error,
        "last_output": output or None,
    }


def _issue(
    kind: str,
    severity: str,
    title: str,
    summary: str,
    suggestion: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "title": title,
        "summary": summary,
        "suggestion": suggestion,
        "facts": facts,
    }


def _explain_health_output(output: str, exit_code: int | None) -> str | None:
    text = (output or "").lower()
    refused = (
        exit_code == 7
        or "connection refused" in text
        or "couldn't connect" in text
        or "could not connect" in text
        or "failed to connect" in text
        or "errno 111" in text
        or "http/0.0 000" in text
        or "000 command failed" in text
    )
    if refused:
        return "The app is running but not answering on its expected port."

    timed_out = (
        exit_code == 28
        or "timed out" in text
        or "timeout" in text
        or "deadline exceeded" in text
    )
    if timed_out:
        return "The health check timed out; the app may be stuck or overloaded."

    db_not_ready = (
        "pg_isready" in text
        or "the database system is starting" in text
        or "database is not yet accepting" in text
        or "database is not ready" in text
        or "waiting for postgresql" in text
        or ("role" in text and "does not exist" in text)
    )
    if db_not_ready or (
        ("postgres" in text or "mysql" in text or "mariadb" in text)
        and any(
            s in text
            for s in ("not ready", "starting up", "connection refused", "can't connect")
        )
    ):
        return "The database is not ready for connections yet."

    if "no space" in text or "enospc" in text or "disk is full" in text:
        return "The server appears to be out of disk space."

    if "permission denied" in text:
        return "The health check was not allowed to run (permission denied)."

    return None


def _unhealthy_issue(state: dict, health: dict, restart_count: int) -> dict[str, Any]:
    last = last_health_entry(health)
    last_output = sanitize_output((last or {}).get("Output"))
    last_code = (last or {}).get("ExitCode")
    why = _explain_health_output(last_output, last_code)
    streak = health.get("FailingStreak") or 0
    if why:
        summary = why
    elif streak:
        times = "time" if streak == 1 else "times"
        summary = f"Docker’s health check failed {streak} {times} in a row."
    else:
        summary = "Docker’s health check is failing."
    facts = _facts(
        state, restart_count=restart_count, health=health, last_output=last_output
    )
    return _issue(
        "unhealthy",
        "error",
        "Not responding",
        summary,
        "Try Restart. If it keeps happening, the app may be stuck or overloaded.",
        facts,
    )


def _crash_copy(exit_code: int | None, error: str | None) -> tuple[str, str, str]:
    if exit_code == 127:
        return (
            "Could not start",
            "The app could not find the command it was supposed to run.",
            "This is usually a bad image or a typo in the start command. Open Inspect to check.",
        )
    if exit_code == 126:
        return (
            "Could not start",
            "The app was not allowed to run its start command (permission denied).",
            "Check the image and volume permissions, then try Start again.",
        )
    if exit_code == 139:
        return (
            "Crashed",
            "The app crashed due to an internal error.",
            "Try Restart. If it happens again, open Logs for details.",
        )
    if error:
        return (
            "Could not start",
            "Docker could not start this app.",
            "Try Start again. If it fails, open Inspect for the error.",
        )
    return (
        "Crashed",
        "The app quit unexpectedly.",
        "Try Restart. If it happens again, open Logs for details.",
    )


def explain_container(attrs: dict | None) -> dict[str, Any] | None:
    """Turn Docker inspect State/Health into a non-technical issue, or None if fine."""
    if not attrs:
        return None
    state = attrs.get("State") or {}
    status = (state.get("Status") or "").lower()
    health = state.get("Health") or {}
    health_status = (health.get("Status") or "").lower()
    restart_count = int(attrs.get("RestartCount") or 0)
    exit_code = state.get("ExitCode")
    oom = bool(state.get("OOMKilled"))
    error = _collapse(state.get("Error") or "") or None
    facts = lambda **extra: _facts(
        state, restart_count=restart_count, health=health, **extra
    )

    # Only Docker's OOMKilled flag is a real OOM. Exit 137 is SIGKILL — compose
    # recreate and stop-timeout use it too. See docs/AUDIT.md.
    if oom:
        return _issue(
            "oom",
            "error",
            "Ran out of memory",
            "This app used too much memory and the system stopped it.",
            "Try Restart. If it happens again, this app may need more memory.",
            facts(),
        )

    if status == "restarting" or (state.get("Restarting") and status != "running"):
        # Exit 0 / 143 (SIGTERM) / 137 (SIGKILL) is a planned stop — queue
        # --max-time, docker restart, compose recreate.
        # Do not label that as a crash loop; mail alerts also ignore a single recycle.
        if exit_code in (0, 143, 137):
            return None
        return _issue(
            "restarting",
            "error",
            "Keeps restarting",
            "This app keeps crashing and Docker is trying to start it again.",
            "A restart may only help briefly. Open Logs to see what happens just before it crashes.",
            facts(),
        )

    if status == "dead" or state.get("Dead"):
        return _issue(
            "dead",
            "error",
            "Stuck",
            "Docker could not fully stop this app.",
            "Try Remove, then start it again from Compose or Apps.",
            facts(),
        )

    if status == "paused" or state.get("Paused"):
        return _issue(
            "paused",
            "warning",
            "Paused",
            "This app is frozen on purpose.",
            "Use Restart if you want it running again.",
            facts(),
        )

    if status in ("exited", "removing"):
        if exit_code in (0, 143, 137):
            title = "Stopped" if status == "exited" else "Being removed"
            summary = (
                "This app was stopped (not a crash)."
                if status == "exited"
                else "This app is being removed."
            )
            suggestion = (
                "Use Start if you want it running again."
                if status == "exited"
                else "Wait a moment, then Refresh."
            )
            return _issue("stopped", "warning", title, summary, suggestion, facts())
        title, summary, suggestion = _crash_copy(exit_code, error)
        return _issue("crashed", "error", title, summary, suggestion, facts())

    if health_status == "unhealthy":
        return _unhealthy_issue(state, health, restart_count)

    return None
