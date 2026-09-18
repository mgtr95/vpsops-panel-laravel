import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import db_svc, laravel_svc
from app.utils import read_json, write_json


DEMO = {
    "id": "demo-app",
    "name": "Demo App",
    "path": "/apps/demo-app",
    "managed": True,
    "addons": {"database": "postgres"},
}


class FakeSettings:
    def __init__(self, root: Path):
        self.panel_config_dir = str(root / "config")
        self.data_dir = str(root / "data")
        Path(self.panel_config_dir).mkdir()
        Path(self.data_dir).mkdir()

    @property
    def databases_config_path(self) -> Path:
        return Path(self.panel_config_dir) / "databases.json"

    @property
    def backups_path(self) -> Path:
        return Path(self.data_dir) / "backups.json"


class StaleIdTests(unittest.TestCase):
    def test_marks_discovered_postgres_id(self):
        configs = [
            {"id": "demo-app-postgres", "engine": "postgres", "host": "legacy-postgres-2"},
            {"id": "demo-app-db", "engine": "postgres", "host": "demo-app_postgres"},
            {"id": "other-postgres", "engine": "postgres", "host": "keep-me"},
        ]
        self.assertEqual(laravel_svc.stale_database_ids(DEMO, configs), ["demo-app-postgres"])

    def test_marks_same_env_file_under_app(self):
        configs = [
            {
                "id": "shop-api-postgres",
                "engine": "postgres",
                "env_file": "/apps/shop-api/.env.prod",
            }
        ]
        app = {
            "id": "shop-api",
            "path": "/apps/shop-api",
            "managed": True,
            "addons": {"database": "postgres"},
        }
        self.assertEqual(laravel_svc.stale_database_ids(app, configs), ["shop-api-postgres"])

    def test_app_id_from_db_id(self):
        self.assertEqual(laravel_svc.app_id_from_db_id("demo-app-postgres"), "demo-app")
        self.assertEqual(laravel_svc.app_id_from_db_id("shop-api-mysql"), "shop-api")
        self.assertEqual(laravel_svc.app_id_from_db_id("demo-app-db"), "demo-app")
        self.assertIsNone(laravel_svc.app_id_from_db_id("onedrive"))


class UpsertRepointTests(unittest.TestCase):
    def test_upsert_drops_old_connection_and_rewrites_backup_job(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = FakeSettings(Path(raw))
            write_json(
                settings.databases_config_path,
                [
                    {
                        "id": "demo-app-postgres",
                        "engine": "postgres",
                        "host": "legacy-postgres-2",
                        "env_file": "/apps/demo-app/.env",
                    }
                ],
            )
            write_json(
                settings.backups_path,
                [
                    {
                        "id": "job1",
                        "name": "Demo App PostgreSQL",
                        "source": {
                            "type": "postgres",
                            "connection_id": "demo-app-postgres",
                            "database": "app_db",
                        },
                    }
                ],
            )
            with (
                patch("app.services.laravel_svc.get_settings", return_value=settings),
                patch("app.services.backup_svc.get_settings", return_value=settings),
            ):
                laravel_svc.upsert_database_config(DEMO)
                dbs = read_json(settings.databases_config_path, [])
                ids = [c["id"] for c in dbs]
                self.assertIn("demo-app-db", ids)
                self.assertNotIn("demo-app-postgres", ids)
                self.assertEqual(dbs[-1]["host"], "demo-app_postgres")
                jobs = read_json(settings.backups_path, [])
                self.assertEqual(jobs[0]["source"]["connection_id"], "demo-app-db")


class ResolveFallbackTests(unittest.TestCase):
    def test_uses_panel_host_when_old_container_is_gone(self):
        cfg = {
            "id": "demo-app-postgres",
            "engine": "postgres",
            "host": "legacy-postgres-2",
            "user": "app_user",
            "database": "app_db",
            "env_file": "/apps/demo-app/.env",
            "env_map": {"host": "DB_HOST", "password": "DB_PASSWORD"},
        }
        with (
            patch("app.services.db_svc.parse_dotenv", return_value={"DB_HOST": "postgres"}),
            patch(
                "app.services.db_svc._peer_running",
                side_effect=lambda host: host == "demo-app_postgres",
            ),
            patch("app.services.db_svc._panel_db_host_for_cfg", return_value="demo-app_postgres"),
        ):
            resolved = db_svc._resolve_postgres(cfg)
        self.assertEqual(resolved["host"], "demo-app_postgres")

    def test_keeps_old_host_while_that_container_still_runs(self):
        cfg = {
            "id": "shop-api-postgres",
            "engine": "postgres",
            "host": "shop_postgres",
            "user": "shop",
            "database": "shop",
        }
        with (
            patch("app.services.db_svc._peer_running", side_effect=lambda host: host == "shop_postgres"),
            patch("app.services.db_svc._panel_db_host_for_cfg", return_value="shop-api_postgres"),
        ):
            resolved = db_svc._resolve_postgres(cfg)
        self.assertEqual(resolved["host"], "shop_postgres")


class DiscoverSkipTests(unittest.TestCase):
    def test_skips_managed_apps(self):
        from app.services import discover_svc

        apps = [
            {"id": "demo-app", "path": "/apps/demo-app", "managed": True},
            {"id": "legacy", "path": "/missing", "managed": False},
        ]
        with patch("app.services.discover_svc.deploy_svc.list_apps", return_value=apps):
            self.assertEqual(discover_svc.discover_databases(), [])
