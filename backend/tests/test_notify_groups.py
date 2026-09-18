import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import notify_groups_svc
from app.utils import write_json


class FakeSettings:
    def __init__(self, root: Path):
        self.data_dir = str(root)

    @property
    def notify_groups_path(self) -> Path:
        return Path(self.data_dir) / "notify_groups.json"

    @property
    def notify_pending_path(self) -> Path:
        return Path(self.data_dir) / "notify_pending.json"

    @property
    def backups_path(self) -> Path:
        return Path(self.data_dir) / "backups.json"


JOBS = [
    {"id": "pg", "name": "Acme PostgreSQL"},
    {"id": "files", "name": "Acme files"},
    {"id": "thumbs", "name": "Acme thumbnails"},
]

GROUP = {
    "id": "grp1",
    "name": "Urudzbeni zapisnik",
    "job_ids": ["pg", "files", "thumbs"],
    "timeout_minutes": 120,
    "notify": {
        "enabled": True,
        "account_id": "mail1",
        "recipient_ids": ["r1"],
        "to_emails": [],
        "on": "always",
    },
}


class GroupNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_message_lists_unfinished_jobs(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = FakeSettings(Path(raw))
            write_json(settings.notify_groups_path, [GROUP])
            write_json(settings.notify_pending_path, {"batches": {}})
            write_json(settings.backups_path, JOBS)
            with (
                patch.object(notify_groups_svc, "get_settings", return_value=settings),
                patch.object(notify_groups_svc.backup_svc, "get_settings", return_value=settings),
                patch.object(notify_groups_svc.backup_svc, "list_backup_jobs", return_value=JOBS),
                patch.object(notify_groups_svc.backup_svc, "scheduler", None),
            ):
                msg = await notify_groups_svc.record_group_run(
                    {"id": "files", "notify": {"group_id": "grp1"}},
                    {"job_id": "files", "status": "success", "log": "ok"},
                )
        self.assertIn("Group email waiting for:", msg)
        self.assertIn("Acme PostgreSQL", msg)
        self.assertIn("Acme thumbnails", msg)
        self.assertNotIn("Acme files", msg.split("waiting for:")[1])

    async def test_manual_job_uses_group_recipients(self):
        account = {"id": "mail1", "from_email": "backup@example.com"}
        targets = [{"email": "ops@example.com", "include_details": True}]
        job = {"id": "files", "name": "Acme files", "notify": {"group_id": "grp1"}}
        entry = {"job_id": "files", "status": "success"}
        with (
            patch.object(notify_groups_svc, "get_group", return_value=GROUP),
            patch.object(notify_groups_svc.mail_svc, "get_account", return_value=account),
            patch.object(notify_groups_svc.mail_svc, "resolve_notify_recipient_targets", return_value=targets),
            patch.object(
                notify_groups_svc.mail_svc,
                "send_backup_email_to_recipients",
                new_callable=AsyncMock,
            ) as send,
        ):
            sent = await notify_groups_svc.notify_manual_job(job, entry)
        self.assertTrue(sent)
        send.assert_awaited_once_with(account, job, entry, targets)
