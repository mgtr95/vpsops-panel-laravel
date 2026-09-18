from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.date import DateTrigger

from app.config import get_settings
from app.services import backup_svc, mail_svc
from app.services.mail_svc import _valid_email
from app.utils import read_json, write_json

DEFAULT_TIMEOUT_MINUTES = 120


def _groups_path():
    return get_settings().notify_groups_path


def _pending_path():
    return get_settings().notify_pending_path


def list_groups() -> list[dict]:
    data = read_json(_groups_path(), [])
    return data if isinstance(data, list) else []


def save_groups(groups: list[dict]) -> None:
    write_json(_groups_path(), groups)


def get_group(group_id: str) -> dict | None:
    for group in list_groups():
        if group.get("id") == group_id:
            return group
    return None


def _normalize_notify(notify: dict | None, existing: dict | None = None) -> dict:
    notify = notify or {}
    existing = existing or {}
    account_id = (notify.get("account_id") if "account_id" in notify else existing.get("account_id") or "").strip()
    on = (notify.get("on") if "on" in notify else existing.get("on") or "always").strip().lower()
    if on not in ("always", "failure", "success"):
        on = "always"

    contact_ids = notify.get("recipient_ids") if "recipient_ids" in notify else notify.get("contact_ids")
    if contact_ids is None:
        contact_ids = existing.get("recipient_ids") or existing.get("contact_ids") or []
    if not isinstance(contact_ids, list):
        contact_ids = []
    contact_ids = [str(c).strip() for c in contact_ids if str(c).strip()]

    to_emails_raw = notify.get("to_emails") if "to_emails" in notify else existing.get("to_emails") or []
    if not isinstance(to_emails_raw, list):
        to_emails_raw = []
    to_emails = [e.strip() for e in to_emails_raw if isinstance(e, str) and e.strip() and _valid_email(e.strip())]

    enabled = bool(notify.get("enabled") if "enabled" in notify else existing.get("enabled", False))
    enabled = enabled and bool(account_id)

    return {
        "enabled": enabled,
        "account_id": account_id,
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


def _load_pending() -> dict[str, Any]:
    data = read_json(_pending_path(), {})
    if not isinstance(data, dict):
        return {"batches": {}}
    batches = data.get("batches")
    if not isinstance(batches, dict):
        data["batches"] = {}
    return data


def _save_pending(data: dict[str, Any]) -> None:
    write_json(_pending_path(), data)


def _timeout_job_id(group_id: str) -> str:
    return f"notify-group-timeout-{group_id}"


def _cancel_timeout(group_id: str) -> None:
    scheduler = backup_svc.scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job(_timeout_job_id(group_id))
    except Exception:
        pass


def _schedule_timeout(group_id: str, timeout_at: datetime) -> None:
    scheduler = backup_svc.scheduler
    if scheduler is None:
        return
    _cancel_timeout(group_id)
    scheduler.add_job(
        _timeout_send,
        trigger=DateTrigger(run_date=timeout_at),
        args=[group_id],
        id=_timeout_job_id(group_id),
        replace_existing=True,
    )


async def _timeout_send(group_id: str) -> None:
    await flush_group_batch(group_id, timed_out=True)


def restore_pending_timeouts() -> None:
    pending = _load_pending()
    now = datetime.now(timezone.utc)
    scheduler = backup_svc.scheduler
    for group_id, batch in (pending.get("batches") or {}).items():
        if not batch or not batch.get("entries"):
            continue
        timeout_at = batch.get("timeout_at")
        try:
            dt = datetime.fromisoformat(str(timeout_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            dt = now
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= now:
            if scheduler is not None:
                scheduler.add_job(
                    _timeout_send,
                    trigger=DateTrigger(run_date=now),
                    args=[group_id],
                    id=f"notify-group-restore-{group_id}",
                    replace_existing=True,
                )
        else:
            _schedule_timeout(group_id, dt)


def _sync_job_membership(group: dict, previous_job_ids: set[str] | None = None) -> None:
    group_id = group["id"]
    job_ids = set(group.get("job_ids") or [])
    previous_job_ids = previous_job_ids or set()
    jobs = backup_svc.list_backup_jobs()
    changed = False
    for job in jobs:
        jid = job.get("id")
        notify = dict(job.get("notify") or {})
        if jid in job_ids:
            if notify.get("group_id") != group_id:
                notify["group_id"] = group_id
                notify["enabled"] = False
                job["notify"] = notify
                changed = True
        elif notify.get("group_id") == group_id and jid in previous_job_ids:
            notify["group_id"] = ""
            job["notify"] = notify
            changed = True
    if changed:
        backup_svc.save_backup_jobs(jobs)


def upsert_group(body: dict) -> dict:
    groups = list_groups()
    existing = None
    group_id = (body.get("id") or "").strip()
    if group_id:
        for item in groups:
            if item.get("id") == group_id:
                existing = item
                break
        if existing is None:
            raise ValueError("Notification group not found.")

    name = (body.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        raise ValueError("Enter a name for this notification group.")

    job_ids_raw = body.get("job_ids") if "job_ids" in body else (existing or {}).get("job_ids") or []
    if not isinstance(job_ids_raw, list):
        job_ids_raw = []
    known_jobs = {j.get("id") for j in backup_svc.list_backup_jobs()}
    job_ids = [str(j).strip() for j in job_ids_raw if str(j).strip() in known_jobs]
    if not job_ids:
        raise ValueError("Select at least one backup for this group.")

    try:
        timeout_minutes = int(
            body.get("timeout_minutes")
            if body.get("timeout_minutes") not in (None, "")
            else (existing or {}).get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES
        )
    except (TypeError, ValueError) as e:
        raise ValueError("Timeout must be a number of minutes.") from e
    timeout_minutes = min(max(timeout_minutes, 5), 24 * 60)

    notify = _normalize_notify(body.get("notify"), (existing or {}).get("notify"))
    if notify.get("enabled") and not notify.get("account_id"):
        raise ValueError("Choose a mail account for this group.")
    if notify.get("enabled"):
        from app.services import mail_svc

        if not mail_svc.resolve_notify_recipients(notify):
            raise ValueError("Choose at least one recipient or add extra email addresses.")

    group = {
        **(existing or {}),
        "name": name,
        "job_ids": job_ids,
        "notify": notify,
        "timeout_minutes": timeout_minutes,
    }
    if not group.get("id"):
        group["id"] = str(uuid.uuid4())[:8]
        group["created_at"] = datetime.now(timezone.utc).isoformat()
    group["updated_at"] = datetime.now(timezone.utc).isoformat()

    previous_job_ids = set((existing or {}).get("job_ids") or [])
    replaced = False
    for i, item in enumerate(groups):
        if item.get("id") == group["id"]:
            groups[i] = group
            replaced = True
            break
    if not replaced:
        groups.append(group)
    save_groups(groups)
    _sync_job_membership(group, previous_job_ids)
    return group


def delete_group(group_id: str) -> bool:
    existing = get_group(group_id)
    groups = list_groups()
    new_groups = [g for g in groups if g.get("id") != group_id]
    if len(new_groups) == len(groups):
        return False
    save_groups(new_groups)
    previous_job_ids = set((existing or {}).get("job_ids") or [])
    _sync_job_membership({"id": group_id, "job_ids": []}, previous_job_ids)
    _cancel_timeout(group_id)
    pending = _load_pending()
    batches = pending.get("batches") or {}
    if group_id in batches:
        del batches[group_id]
        _save_pending(pending)
    return True


def remove_recipient_from_groups(recipient_id: str) -> None:
    groups = list_groups()
    changed = False
    for group in groups:
        notify = group.get("notify") or {}
        ids = notify.get("recipient_ids") or notify.get("contact_ids") or []
        if recipient_id in ids:
            key = "recipient_ids" if "recipient_ids" in notify else "contact_ids"
            notify[key] = [i for i in ids if i != recipient_id]
            group["notify"] = notify
            changed = True
    if changed:
        save_groups(groups)


def clear_notify_for_account(account_id: str) -> None:
    groups = list_groups()
    changed = False
    for group in groups:
        notify = group.get("notify") or {}
        if notify.get("account_id") == account_id:
            group["notify"] = {**notify, "enabled": False, "account_id": ""}
            changed = True
    if changed:
        save_groups(groups)


def remove_job_from_groups(job_id: str) -> None:
    groups = list_groups()
    changed = False
    for group in groups:
        ids = group.get("job_ids") or []
        if job_id in ids:
            group["job_ids"] = [j for j in ids if j != job_id]
            changed = True
    if changed:
        save_groups(groups)


def _get_batch(group_id: str) -> dict | None:
    pending = _load_pending()
    return (pending.get("batches") or {}).get(group_id)


def _set_batch(group_id: str, batch: dict | None) -> None:
    pending = _load_pending()
    batches = pending.setdefault("batches", {})
    if batch is None:
        batches.pop(group_id, None)
        _cancel_timeout(group_id)
    else:
        batches[group_id] = batch
    _save_pending(pending)


async def record_group_run(job: dict, entry: dict) -> str | None:
    notify = job.get("notify") or {}
    group_id = (notify.get("group_id") or "").strip()
    if not group_id:
        return None

    group = get_group(group_id)
    if not group or not (group.get("notify") or {}).get("enabled"):
        return None

    batch = _get_batch(group_id)
    now = datetime.now(timezone.utc)
    job_id = job.get("id") or entry.get("job_id")

    if not batch or not batch.get("entries"):
        timeout_minutes = int(group.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES)
        timeout_at = now + timedelta(minutes=timeout_minutes)
        batch = {
            "batch_id": str(uuid.uuid4())[:8],
            "started_at": now.isoformat(),
            "timeout_at": timeout_at.isoformat(),
            "entries": {},
        }
        _schedule_timeout(group_id, timeout_at)

    entries = dict(batch.get("entries") or {})
    entries[job_id] = entry
    batch["entries"] = entries
    _set_batch(group_id, batch)

    expected = set(group.get("job_ids") or [])
    if expected.issubset(set(entries.keys())):
        return await _send_batch(group, batch, timed_out=False)
    jobs_by_id = {j.get("id"): j for j in backup_svc.list_backup_jobs()}
    missing = [
        (jobs_by_id[jid].get("name") if jid in jobs_by_id else jid) or jid
        for jid in (group.get("job_ids") or [])
        if jid not in entries
    ]
    timeout = int(group.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES)
    if missing:
        return (
            f"Group email waiting for: {', '.join(missing)} "
            f"(sends when they finish, or after {timeout} min)."
        )
    return None


async def notify_manual_job(job: dict, entry: dict) -> bool:
    """Send a per-job status email for a Run click, even if the job is in a group."""
    notify = job.get("notify") or {}
    group_id = (notify.get("group_id") or "").strip()
    group = get_group(group_id) if group_id else None
    source = (group.get("notify") if group else None) or notify
    if not source.get("enabled"):
        return False
    if not mail_svc.should_notify_group(source, [entry.get("status")]):
        return False
    account = mail_svc.get_account(source.get("account_id"))
    if not account:
        raise ValueError("The selected mail account is no longer set up. Open Mail and add it again.")
    targets = mail_svc.resolve_notify_recipient_targets(source)
    if not targets:
        raise ValueError("Choose at least one recipient on the Mail page, or add extra emails.")
    await mail_svc.send_backup_email_to_recipients(account, job, entry, targets)
    return True


async def flush_group_batch(group_id: str, timed_out: bool = False) -> str | None:
    batch = _get_batch(group_id)
    if not batch or not batch.get("entries"):
        _set_batch(group_id, None)
        return None
    group = get_group(group_id)
    if not group:
        _set_batch(group_id, None)
        return None
    return await _send_batch(group, batch, timed_out=timed_out)


async def _send_batch(group: dict, batch: dict, timed_out: bool) -> str | None:
    group_id = group["id"]
    entries_map = batch.get("entries") or {}
    job_ids = group.get("job_ids") or []
    jobs = {j.get("id"): j for j in backup_svc.list_backup_jobs()}
    entries = [entries_map[jid] for jid in job_ids if jid in entries_map]
    missing = [jobs[jid]["name"] for jid in job_ids if jid not in entries_map and jid in jobs]

    if not entries:
        _set_batch(group_id, None)
        return None

    notify = group.get("notify") or {}
    statuses = [e.get("status") for e in entries]
    if not mail_svc.should_notify_group(notify, statuses):
        _set_batch(group_id, None)
        return "Skipped (notification rules)."

    try:
        await mail_svc.notify_backup_group(group, entries, jobs, missing if timed_out else None)
        msg = "Group status email sent."
        if timed_out and missing:
            msg += f" ({len(missing)} backup(s) not yet run.)"
    except Exception as e:
        msg = f"Could not send group email: {e}"
    _set_batch(group_id, None)
    return msg
