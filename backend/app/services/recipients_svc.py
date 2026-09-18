from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.services.mail_svc import _valid_email
from app.utils import read_json, write_json


def _path() -> Path:
    return get_settings().recipients_path


def _legacy_path() -> Path:
    return get_settings().notify_contacts_path


def _migrate_legacy() -> None:
    path = _path()
    if path.is_file():
        return
    legacy = _legacy_path()
    if legacy.is_file():
        data = read_json(legacy, [])
        if isinstance(data, list):
            write_json(path, data)


def list_recipients() -> list[dict]:
    _migrate_legacy()
    data = read_json(_path(), [])
    return data if isinstance(data, list) else []


def save_recipients(recipients: list[dict]) -> None:
    write_json(_path(), recipients)


def get_recipient(recipient_id: str) -> dict | None:
    for item in list_recipients():
        if item.get("id") == recipient_id:
            return item
    return None


def upsert_recipient(body: dict) -> dict:
    recipients = list_recipients()
    existing = None
    recipient_id = (body.get("id") or "").strip()
    if recipient_id:
        for item in recipients:
            if item.get("id") == recipient_id:
                existing = item
                break
        if existing is None:
            raise ValueError("Recipient not found.")

    name = (body.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        raise ValueError("Enter a name for this recipient.")

    email = (body.get("email") or (existing or {}).get("email") or "").strip()
    if not _valid_email(email):
        raise ValueError("Enter a valid email address.")

    if "include_details" in body:
        include_details = bool(body.get("include_details"))
    else:
        include_details = (existing or {}).get("include_details", True)
        if not isinstance(include_details, bool):
            include_details = True

    recipient = {
        **(existing or {}),
        "name": name,
        "email": email,
        "include_details": include_details,
    }
    if not recipient.get("id"):
        recipient["id"] = str(uuid.uuid4())[:8]
        recipient["created_at"] = datetime.now(timezone.utc).isoformat()
    recipient["updated_at"] = datetime.now(timezone.utc).isoformat()

    replaced = False
    for i, item in enumerate(recipients):
        if item.get("id") == recipient["id"]:
            recipients[i] = recipient
            replaced = True
            break
    if not replaced:
        recipients.append(recipient)
    save_recipients(recipients)
    return recipient


def delete_recipient(recipient_id: str) -> bool:
    recipients = list_recipients()
    new_recipients = [r for r in recipients if r.get("id") != recipient_id]
    if len(new_recipients) == len(recipients):
        return False
    save_recipients(new_recipients)

    from app.services import backup_svc, notify_groups_svc

    backup_svc.remove_recipient_from_jobs(recipient_id)
    notify_groups_svc.remove_recipient_from_groups(recipient_id)
    return True
