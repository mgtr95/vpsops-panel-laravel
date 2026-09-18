import unittest
from unittest.mock import AsyncMock, patch

from app.services import mail_svc


class SendTestRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_account_test_sends_to_from_email(self):
        account = {
            "id": "acct1",
            "name": "Ops",
            "from_email": "backup@example.com",
            "password_encrypted": "token",
        }
        with (
            patch.object(mail_svc, "get_account", return_value=account),
            patch.object(mail_svc, "send_message", new_callable=AsyncMock) as send,
            patch.object(mail_svc, "_record_test") as record,
        ):
            result = await mail_svc.send_test("acct1")

        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs.get("to_email"), "backup@example.com")
        record.assert_called_once_with("acct1", ok=True)
        self.assertEqual(result["to"], "backup@example.com")

    def test_send_message_sync_requires_an_explicit_recipient(self):
        with self.assertRaises(ValueError) as ctx:
            mail_svc.send_message_sync(
                {"from_email": "backup@example.com", "password_encrypted": "token"},
                "subject",
                "text",
                "<p>html</p>",
            )
        self.assertIn("recipient", str(ctx.exception).lower())


class BuildBackupEmailTests(unittest.TestCase):
    def test_includes_sync_stats_for_file_jobs(self):
        job = {
            "name": "Zuc files",
            "destination": "onedrive:Backups/zuc",
            "source": {"type": "app_files"},
            "timezone": "UTC",
        }
        entry = {
            "status": "success",
            "started_at": "2026-01-01T12:00:00+00:00",
            "finished_at": "2026-01-01T12:01:00+00:00",
            "log": "ok",
            "sync_stats": {
                "transfers": 2,
                "checks": 100,
                "deletes": 0,
                "bytes": 2048,
                "errors": 0,
                "elapsedTime": 45.0,
            },
        }
        subject, text, html = mail_svc.build_backup_email(job, entry)
        self.assertIn("Zuc files", subject)
        self.assertIn("Uploaded or updated: 2 files", text)
        self.assertIn("Unchanged: 100 files", text)
        self.assertIn("Uploaded or updated", html)
        self.assertNotIn("Uploaded or updated", mail_svc.build_backup_email(job, entry, include_details=False)[1])
