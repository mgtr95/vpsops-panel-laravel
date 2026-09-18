import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import backup_svc, rclone_svc


def _tree(root: Path) -> None:
    (root / "storage/app/public/attachments").mkdir(parents=True)
    (root / "storage/app/public/attachments/doc.pdf").write_bytes(b"pdf")
    (root / "storage/app/private").mkdir(parents=True)
    (root / "storage/framework/cache").mkdir(parents=True)
    (root / "storage/logs").mkdir(parents=True)
    (root / "public/uploads").mkdir(parents=True)


class RelPathTests(unittest.TestCase):
    def test_normalizes_slashes_and_dots(self):
        self.assertEqual(backup_svc.normalize_rel_path("/storage/app/public/attachments/"), "storage/app/public/attachments")
        self.assertEqual(backup_svc.normalize_rel_path("storage/./app"), "storage/app")
        self.assertEqual(backup_svc.normalize_rel_path(""), "")

    def test_rejects_parent_segments(self):
        with self.assertRaises(ValueError):
            backup_svc.normalize_rel_path("storage/../etc")

    def test_resolve_stays_under_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _tree(root)
            src = backup_svc.resolve_app_path(root, "storage/app/public/attachments")
            self.assertEqual(src, (root / "storage/app/public/attachments").resolve())

    def test_dedupes_paths(self):
        paths = backup_svc.normalize_app_file_paths(
            ["storage/app/public/attachments/", "storage/app/public/attachments"],
            extra="storage/app/public/attachments",
        )
        self.assertEqual(paths, ["storage/app/public/attachments"])


class DiscoverFoldersTests(unittest.TestCase):
    def test_lists_nested_attachments_not_framework(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _tree(root)
            folders = backup_svc.discover_app_folders(root)
            paths = [f["path"] for f in folders]
            self.assertIn("storage/app", paths)
            self.assertIn("storage/app/public", paths)
            self.assertIn("storage/app/public/attachments", paths)
            self.assertIn("storage/app/private", paths)
            self.assertNotIn("storage/framework/cache", paths)
            self.assertFalse(any(f["selected"] for f in folders))

    def test_browse_children(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _tree(root)
            app = {"id": "acme", "path": str(root)}
            with patch.object(backup_svc.deploy_svc, "get_app", return_value=app):
                listed = backup_svc.list_app_source_dirs("acme", "storage/app/public")
            names = [d["name"] for d in listed["dirs"]]
            self.assertEqual(listed["path"], "storage/app/public")
            self.assertEqual(listed["parent"], "storage/app")
            self.assertIn("attachments", names)


class AppFilesSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_folder_contents_into_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _tree(root)
            settings_dir = Path(raw) / "panel-data"
            settings_dir.mkdir()
            job = {
                "id": "job1",
                "name": "Acme files",
                "destination": "onedrive:Backups/acme",
                "source": {
                    "type": "app_files",
                    "app_id": "acme",
                    "paths": ["storage/app/public/attachments"],
                },
                "notify": {},
            }
            app = {"id": "acme", "path": str(root)}

            class Settings:
                data_dir = str(settings_dir)

                @property
                def backups_path(self):
                    return Path(self.data_dir) / "backups.json"

                @property
                def backup_history_path(self):
                    return Path(self.data_dir) / "backup_history.json"

            sample_stats = {
                "transfers": 2,
                "checks": 10,
                "deletes": 0,
                "bytes": 4096,
                "errors": 0,
                "elapsedTime": 3.5,
            }
            with (
                patch.object(backup_svc, "get_backup_job", return_value=job),
                patch.object(backup_svc, "get_settings", return_value=Settings()),
                patch.object(backup_svc.deploy_svc, "get_app", return_value=app),
                patch.object(
                    backup_svc.rclone_svc,
                    "sync_and_stats",
                    new_callable=AsyncMock,
                    return_value=sample_stats,
                ) as sync,
                patch.object(backup_svc, "_append_history"),
                patch("app.services.mail_svc.should_notify", return_value=False),
            ):
                result = await backup_svc.run_backup_job("job1", manual=True)

            self.assertEqual(result["status"], "success")
            sync.assert_awaited_once()
            kwargs = sync.await_args.kwargs
            self.assertEqual(kwargs["dst_fs"], "onedrive:Backups/acme")
            self.assertTrue(str(kwargs["src_fs"]).endswith("storage/app/public/attachments"))
            self.assertTrue(kwargs["group"].startswith("panel-backup-"))
            self.assertEqual(result["sync_stats"], sample_stats)
            self.assertIn("Sync summary:", result["log"])


class SyncStatsMergeTests(unittest.TestCase):
    def test_merge_sums_folder_runs(self):
        merged = rclone_svc.merge_sync_stats(
            [
                {"transfers": 1, "checks": 5, "deletes": 0, "bytes": 100, "errors": 0, "elapsedTime": 2.0},
                {"transfers": 3, "checks": 7, "deletes": 1, "bytes": 200, "errors": 0, "elapsedTime": 4.0},
            ]
        )
        self.assertEqual(merged["transfers"], 4)
        self.assertEqual(merged["checks"], 12)
        self.assertEqual(merged["deletes"], 1)
        self.assertEqual(merged["bytes"], 300)
        self.assertEqual(merged["elapsedTime"], 6.0)


class UpsertPathsTests(unittest.TestCase):
    def test_requires_a_folder(self):
        with self.assertRaises(ValueError):
            backup_svc.upsert_backup_job(
                {
                    "name": "Files",
                    "kind": "files",
                    "source": {"type": "app_files", "app_id": "acme", "paths": []},
                    "destination_remote": "onedrive",
                    "destination_folder": "Backups/acme",
                }
            )

    def test_stores_normalized_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            settings_dir = Path(raw)

            class Settings:
                data_dir = str(settings_dir)
                tz = "UTC"

                @property
                def backups_path(self):
                    return Path(self.data_dir) / "backups.json"

                @property
                def config_path(self):
                    return Path(self.data_dir) / "config.json"

            with (
                patch.object(backup_svc, "get_settings", return_value=Settings()),
                patch.object(backup_svc, "reschedule"),
            ):
                job = backup_svc.upsert_backup_job(
                    {
                        "name": "Attachments",
                        "kind": "files",
                        "source": {
                            "type": "app_files",
                            "app_id": "acme",
                            "paths": ["/storage/app/public/attachments/"],
                        },
                        "destination_remote": "onedrive",
                        "destination_folder": "Backups/acme",
                    }
                )
            self.assertEqual(job["source"]["paths"], ["storage/app/public/attachments"])
            self.assertEqual(job["destination"], "onedrive:Backups/acme")
