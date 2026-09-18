import unittest
from pathlib import Path

from app.services.adopt_data_svc import (
    mysql_wipe_sql,
    postgres_wipe_sql,
    same_engine,
    sqlite_consistent_copy,
    sqlite_writer_names,
    snapshot_source,
    storage_dest_for_source,
    storage_host_dir_for_source,
    strip_definers,
    summarize_source,
    _old_compose_file,
)
from app.services.deploy_svc import uses_laravel_pipeline
from app.services.laravel_svc import (
    compose_uses_named_storage,
    dockerignore_text,
    dockerfile_text,
    ensure_host_storage_dirs,
    env_values,
    generate_compose,
    infer_addons,
    normalize_addons,
    normalize_php,
    panel_compose_cmd,
    php_ini_text,
    php_version_from_composer,
    restart_containers_for,
    runtime_services_to_stop,
    write_php_ini,
    PHP_INI_MOUNT,
)
from app.services.pipeline_svc import docker_prod_build_cmd
from app.services import site_svc


class AddonTests(unittest.TestCase):
    def test_normalize_queue_implies_sqlite(self):
        addons = normalize_addons({"queue": True, "database": "none"})
        self.assertEqual(addons["database"], "sqlite")
        self.assertTrue(addons["queue"])
        self.assertFalse(addons["mailpit"])

    def test_php_from_composer_json_minimum(self):
        self.assertEqual(
            php_version_from_composer({"require": {"php": "^8.2", "laravel/framework": "^12.0"}}),
            "8.3",
        )

    def test_php_from_lockfile_raises_to_8_4(self):
        composer = {"require": {"php": "^8.2", "laravel/framework": "^12.0"}}
        lock = {"packages": [{"name": "symfony/clock", "require": {"php": ">=8.4"}}]}
        self.assertEqual(php_version_from_composer(composer, lock), "8.4")

    def test_packages_sanitized(self):
        addons = normalize_addons(
            {
                "packages": "tesseract-ocr imagemagick;wget",
                "php_extensions": "imagick",
            }
        )
        self.assertEqual(addons["packages"], ["tesseract-ocr"])
        self.assertEqual(addons["php_extensions"], ["imagick"])

    def test_infer_postgres_from_env(self):
        path = Path("/tmp/does-not-exist-laravel-infer")
        addons = infer_addons(path)
        self.assertEqual(addons["database"], "none")


class TemplateTests(unittest.TestCase):
    def test_dockerfile_includes_extras_and_http_only(self):
        text = dockerfile_text(
            "8.3",
            {"packages": "tesseract-ocr", "php_extensions": "imagick"},
        )
        self.assertIn("SERVER_NAME=:80", text)
        self.assertIn("tesseract-ocr", text)
        self.assertIn("imagick", text)
        self.assertIn("dunglas/frankenphp", text)
        self.assertIn("TLS stays on the VPS nginx", text)
        self.assertIn("curl -fsS -o /dev/null http://127.0.0.1/up", text)
        self.assertIn("curl -fsS -o /dev/null http://127.0.0.1/", text)
        self.assertIn('pattern="PDF"', text)

    def test_runtime_stop_leaves_database(self):
        self.assertEqual(
            runtime_services_to_stop({"queue": True, "scheduler": True, "database": "postgres"}),
            ["app", "queue", "scheduler"],
        )
        self.assertEqual(runtime_services_to_stop({"database": "postgres"}), ["app"])

    def test_image_build_passes_php_version(self):
        cmd = docker_prod_build_cmd("demo-app", "8.3", extra_tags=["panel-demo-app:latest"])
        self.assertIn("--build-arg", cmd)
        self.assertIn("PHP_VERSION=8.3", cmd)
        self.assertNotIn("PHP_VERSION=8.2", cmd)

    def test_compose_mailpit_and_queue(self):
        compose = generate_compose(
            "demo-app",
            {"database": "postgres", "redis": True, "queue": True, "mailpit": True},
            "vps_proxy",
        )
        services = compose["services"]
        self.assertEqual(services["app"]["container_name"], "demo-app_app")
        self.assertEqual(services["app"]["environment"]["SERVER_NAME"], ":80")
        self.assertIn("mailpit", services)
        self.assertEqual(services["mailpit"]["container_name"], "demo-app_mailpit")
        self.assertIn("queue", services)
        self.assertEqual(compose["networks"]["proxy"]["name"], "vps_proxy")
        self.assertNotIn("scheduler", services)
        self.assertEqual(compose["name"], "panel-demo-app")
        health = services["app"]["healthcheck"]["test"]
        self.assertEqual(health[0], "CMD-SHELL")
        self.assertIn("/up", health[1])
        self.assertIn("127.0.0.1/", health[1])
        pg_health = services["postgres"]["healthcheck"]["test"]
        self.assertEqual(pg_health[0], "CMD-SHELL")
        self.assertIn("-d ${DB_DATABASE:-laravel}", pg_health[1])
        self.assertIn("-U ${DB_USERNAME:-laravel}", pg_health[1])
        self.assertEqual(services["app"]["networks"], ["proxy", "internal"])
        self.assertEqual(services["postgres"]["networks"], ["internal"])
        self.assertEqual(services["redis"]["networks"], ["internal"])
        self.assertEqual(services["queue"]["networks"], ["internal"])
        self.assertEqual(services["mailpit"]["networks"], ["internal"])

    def test_env_mailpit_and_existing_db_none(self):
        values = env_values(
            "shop-api",
            {"database": "none", "mailpit": True},
            "shop.example.com",
            {"DB_CONNECTION": "pgsql", "DB_HOST": "shop_postgres", "DB_PASSWORD": "secret"},
        )
        self.assertEqual(values["DB_HOST"], "shop_postgres")
        self.assertEqual(values["MAIL_HOST"], "mailpit")
        self.assertEqual(values["MAIL_PORT"], "1025")
        self.assertEqual(values["APP_URL"], "https://shop.example.com")
        self.assertEqual(values["ASSET_URL"], "https://shop.example.com")

    def test_env_keeps_custom_https_asset_url(self):
        values = env_values(
            "demo-app",
            {},
            "demo.example.com",
            {"ASSET_URL": "https://cdn.example.com"},
        )
        self.assertEqual(values["ASSET_URL"], "https://cdn.example.com")

    def test_env_without_domain_skips_asset_url(self):
        values = env_values("demo-app", {}, "", {})
        self.assertEqual(values["APP_URL"], "http://demo-app_app")
        self.assertNotIn("ASSET_URL", values)

    def test_caddyfile_trusts_private_proxies(self):
        text = (Path(__file__).resolve().parents[1] / "app/templates/laravel/Caddyfile").read_text()
        self.assertIn("trusted_proxies static private_ranges", text)
        self.assertIn("env HTTPS on", text)
        self.assertIn("auto_https off", text)

    def test_compose_binds_caddyfile(self):
        compose = generate_compose("demo-app", {"database": "none"}, "vps_proxy")
        self.assertIn(
            "./.panel/Caddyfile:/etc/frankenphp/Caddyfile:ro",
            compose["services"]["app"]["volumes"],
        )
        self.assertNotIn("volumes", compose)

    def test_compose_binds_php_ini(self):
        compose = generate_compose(
            "demo-app",
            {"database": "postgres", "queue": True, "scheduler": True},
            "vps_proxy",
        )
        self.assertIn(PHP_INI_MOUNT, compose["services"]["app"]["volumes"])
        self.assertIn(PHP_INI_MOUNT, compose["services"]["queue"]["volumes"])
        self.assertIn(PHP_INI_MOUNT, compose["services"]["scheduler"]["volumes"])

    def test_normalize_php_defaults_and_post_ge_upload(self):
        defaults = normalize_php(None)
        self.assertEqual(defaults["upload_max_filesize_mb"], 20)
        self.assertEqual(defaults["post_max_size_mb"], 30)
        self.assertEqual(defaults["memory_limit_mb"], 512)
        self.assertEqual(defaults["client_max_body_size_mb"], 32)
        bumped = normalize_php({"upload_max_filesize_mb": 50, "post_max_size_mb": 40})
        self.assertEqual(bumped["upload_max_filesize_mb"], 50)
        self.assertEqual(bumped["post_max_size_mb"], 50)
        self.assertEqual(bumped["client_max_body_size_mb"], 50)

    def test_php_ini_text_and_write(self):
        text = php_ini_text({"upload_max_filesize_mb": 20, "post_max_size_mb": 30})
        self.assertIn("upload_max_filesize = 20M", text)
        self.assertIn("post_max_size = 30M", text)
        self.assertIn("memory_limit = 512M", text)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            dest = write_php_ini(path, {"upload_max_filesize_mb": 25, "post_max_size_mb": 35})
            self.assertEqual(dest, path / ".panel" / "php" / "zz-panel.ini")
            self.assertIn("upload_max_filesize = 25M", dest.read_text())

    def test_nginx_vhost_uses_app_php_body_size(self):
        text = site_svc._vhost_https("demo.example.com", "demo-app", True, 48)
        self.assertIn("client_max_body_size 48m;", text)
        self.assertNotIn("client_max_body_size 32m;", text)

    def test_compose_bind_mounts_host_storage(self):
        compose = generate_compose(
            "demo-app",
            {"database": "postgres", "queue": True},
            "vps_proxy",
        )
        expected = [
            "./storage/app:/app/storage/app",
            "./storage/logs:/app/storage/logs",
            "./public/uploads:/app/public/uploads",
        ]
        for spec in expected:
            self.assertIn(spec, compose["services"]["app"]["volumes"])
            self.assertIn(spec, compose["services"]["queue"]["volumes"])
        self.assertNotIn("storage:/app/storage/app", compose["services"]["app"]["volumes"])
        self.assertNotIn("logs:/app/storage/logs", compose["services"]["app"]["volumes"])
        self.assertIn("postgres_data", compose["volumes"])
        self.assertNotIn("storage", compose["volumes"])
        self.assertNotIn("logs", compose["volumes"])
        self.assertFalse(compose_uses_named_storage(compose))

    def test_compose_bind_mounts_sqlite_database(self):
        compose = generate_compose(
            "demo-app",
            {"database": "sqlite", "queue": True},
            "vps_proxy",
        )
        spec = "./database:/app/database"
        self.assertIn(spec, compose["services"]["app"]["volumes"])
        self.assertIn(spec, compose["services"]["queue"]["volumes"])
        self.assertNotIn("database:/app/database", compose["services"]["app"]["volumes"])
        self.assertNotIn("volumes", compose)
        self.assertFalse(compose_uses_named_storage(compose))

    def test_named_storage_volume_is_detected(self):
        self.assertTrue(
            compose_uses_named_storage(
                {
                    "services": {"app": {"volumes": ["storage:/app/storage/app"]}},
                    "volumes": {"storage": {}},
                }
            )
        )
        self.assertFalse(
            compose_uses_named_storage(
                {"services": {"app": {"volumes": ["./storage/app:/app/storage/app"]}}}
            )
        )
        self.assertTrue(
            compose_uses_named_storage(
                {"services": {"app": {"volumes": ["database:/app/database"]}}}
            )
        )

    def test_ensure_host_storage_dirs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            ensure_host_storage_dirs(path)
            self.assertTrue((path / "storage" / "app").is_dir())
            self.assertTrue((path / "storage" / "logs").is_dir())
            self.assertTrue((path / "public" / "uploads").is_dir())
            self.assertTrue((path / "database").is_dir())

    def test_restart_containers(self):
        names = restart_containers_for(
            "demo-app",
            {"database": "mysql", "redis": True, "queue": True, "mailpit": True},
        )
        self.assertEqual(
            names,
            [
                "demo-app_app",
                "demo-app_mysql",
                "demo-app_redis",
                "demo-app_queue",
                "demo-app_mailpit",
            ],
        )


class PipelineFlagTests(unittest.TestCase):
    def test_github_laravel_uses_pipeline(self):
        self.assertTrue(
            uses_laravel_pipeline({"kind": "laravel", "source": "github", "compose_file": "x"})
        )

    def test_managed_local_uses_pipeline(self):
        self.assertTrue(
            uses_laravel_pipeline(
                {"kind": "laravel", "source": "local", "managed": True, "compose_file": "docker-compose.panel.yml"}
            )
        )

    def test_discovered_fpm_does_not(self):
        self.assertFalse(
            uses_laravel_pipeline(
                {"kind": "laravel", "source": "discovered", "compose_file": "docker-compose.yml"}
            )
        )


class AdoptDataTests(unittest.TestCase):
    def test_same_engine_treats_mariadb_as_mysql(self):
        self.assertTrue(same_engine("mariadb", "mysql"))
        self.assertFalse(same_engine("mysql", "postgres"))
        self.assertFalse(same_engine("mysql", "none"))

    def test_strip_definers(self):
        sql = "CREATE DEFINER=`app`@`%` TRIGGER t BEFORE INSERT ON x FOR EACH ROW BEGIN END;"
        self.assertNotIn("DEFINER=", strip_definers(sql))

    def test_dockerignore_skips_backups(self):
        text = dockerignore_text()
        self.assertIn(".panel-backups", text)
        self.assertIn("storage", text)
        self.assertIn("public/uploads", text)
        self.assertIn("database/*.sqlite", text)
        self.assertIn(".panel/php", text)
        self.assertIn(".panel/php/**", text)
        # Whole .panel/ must stay in the build context (Caddyfile + entrypoint COPY).
        self.assertNotRegex(text, r"(?m)^\.panel$")

    def test_panel_compose_uses_isolated_project(self):
        cmd = panel_compose_cmd("demo-app", "up", "-d", "--no-build")
        self.assertIn("-p", cmd)
        self.assertIn("panel-demo-app", cmd)

    def test_wipe_sql_is_idempotent_for_retries(self):
        self.assertIn("DROP SCHEMA public CASCADE", postgres_wipe_sql())
        self.assertIn("CREATE SCHEMA public", postgres_wipe_sql())
        sql = mysql_wipe_sql("app_db")
        self.assertIn("DROP DATABASE IF EXISTS `app_db`", sql)
        self.assertIn("CREATE DATABASE `app_db`", sql)
        with self.assertRaises(RuntimeError):
            mysql_wipe_sql("app_db; drop table users")

    def test_app_unhealthy_is_ready_for_data_copy(self):
        from app.services.adopt_data_svc import container_ready

        self.assertTrue(container_ready("running", "unhealthy", require_healthy=False))
        self.assertFalse(container_ready("running", "unhealthy", require_healthy=True))
        self.assertTrue(container_ready("running", "healthy", require_healthy=True))
        self.assertFalse(container_ready("restarting", "unhealthy", require_healthy=False))

    def test_summarize_external_mysql(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            (path / ".env").write_text(
                "DB_CONNECTION=mysql\nDB_HOST=mariadb\nDB_PORT=3306\n"
                "DB_DATABASE=shop\nDB_USERNAME=shop\nDB_PASSWORD=secret\n"
            )
            (path / "docker-compose.yml").write_text(
                "services:\n"
                "  app:\n"
                "    container_name: shop_app\n"
                "  mariadb:\n"
                "    image: mariadb:11\n"
                "    container_name: shop_db\n"
            )
            summary = summarize_source({"id": "shop-api", "name": "Shop", "path": str(path)})
            self.assertIsNotNone(summary)
            self.assertEqual(summary["engine"], "mysql")
            self.assertEqual(summary["database"], "shop")
            self.assertEqual(summary["host"], "shop_db")
            self.assertNotIn("password", summary)

    def test_sqlite_volume_uses_container_path(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            (path / ".env").write_text("DB_CONNECTION=sqlite\n")
            (path / "docker-compose.prod.yml").write_text(
                "services:\n"
                "  app:\n"
                "    container_name: legacy_app\n"
                "    volumes:\n"
                "      - database:/app/database\n"
                "volumes:\n"
                "  database: {}\n"
            )
            snap = snapshot_source({"id": "demo-app", "name": "Demo", "path": str(path)})
            self.assertIsNotNone(snap)
            self.assertEqual(snap["engine"], "sqlite")
            self.assertEqual(snap["container"], "legacy_app")
            self.assertEqual(snap["path"], "/app/database/database.sqlite")

    def test_old_compose_prefers_prod_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            (path / "docker-compose.yml").write_text("services: {}\n")
            (path / "docker-compose.prod.yml").write_text("services: {}\n")
            self.assertEqual(_old_compose_file(path).name, "docker-compose.prod.yml")

    def test_sqlite_writer_names(self):
        self.assertEqual(
            sqlite_writer_names("demo-app_app"),
            ["demo-app_app", "demo-app_queue", "demo-app_scheduler"],
        )

    def test_storage_dest_maps_public_volume(self):
        self.assertEqual(
            storage_dest_for_source("/app/storage/app/public"),
            "/app/storage/app/public",
        )
        self.assertEqual(storage_dest_for_source("/app/storage/app"), "/app/storage/app")
        self.assertEqual(storage_dest_for_source("/app/storage/logs"), "/app/storage/logs")
        self.assertEqual(storage_dest_for_source("/app/database"), "/app/database")

    def test_storage_host_dir_maps_container_paths(self):
        root = Path("/apps/acme-app")
        self.assertEqual(
            storage_host_dir_for_source(root, "/app/storage/app/public"),
            root / "storage" / "app" / "public",
        )
        self.assertEqual(
            storage_host_dir_for_source(root, "/app/storage/app"),
            root / "storage" / "app",
        )
        self.assertEqual(
            storage_host_dir_for_source(root, "/app/public/uploads"),
            root / "public" / "uploads",
        )
        self.assertEqual(
            storage_host_dir_for_source(root, "/app/database"),
            root / "database",
        )

    def test_sqlite_consistent_copy_keeps_rows(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw:
            src = Path(raw) / "db.sqlite"
            dest = Path(raw) / "out.sqlite"
            conn = sqlite3.connect(src)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            conn.close()
            sqlite_consistent_copy(src, dest)
            self.assertEqual(sqlite3.connect(dest).execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_write_dotenv_keeps_existing_mail(self):
        import tempfile
        from pathlib import Path

        from app.utils import write_dotenv

        with tempfile.TemporaryDirectory() as raw:
            env = Path(raw) / ".env"
            example = Path(raw) / ".env.example"
            env.write_text("MAIL_HOST=smtp.gmail.com\nMAIL_PASSWORD=secret\nAPP_KEY=old\n")
            example.write_text("MAIL_HOST=mailpit\nMAIL_PASSWORD=\nAPP_KEY=\n")
            write_dotenv(env, {"APP_KEY": "new-key", "APP_ENV": "production"}, example)
            text = env.read_text()
            self.assertIn("MAIL_HOST=smtp.gmail.com", text)
            self.assertIn("MAIL_PASSWORD=secret", text)
            self.assertIn("APP_KEY=new-key", text)
            self.assertIn("APP_ENV=production", text)
            self.assertNotIn("mailpit", text)

    def test_write_dotenv_uses_example_when_missing(self):
        import tempfile
        from pathlib import Path

        from app.utils import write_dotenv

        with tempfile.TemporaryDirectory() as raw:
            env = Path(raw) / ".env"
            example = Path(raw) / ".env.example"
            example.write_text("MAIL_HOST=mailpit\nAPP_KEY=\n")
            write_dotenv(env, {"APP_KEY": "new-key"}, example)
            text = env.read_text()
            self.assertIn("MAIL_HOST=mailpit", text)
            self.assertIn("APP_KEY=new-key", text)


class AdoptFormTests(unittest.TestCase):
    def test_form_stays_open_after_failed_switch(self):
        from app.services.pipeline_svc import show_adopt_form

        failed = {
            "kind": "laravel",
            "managed": True,
            "id": "demo-app",
            "pending_adopt": {"migrate_data": True},
        }
        self.assertTrue(show_adopt_form(failed, {"status": "failed", "trigger": "adopt"}))
        self.assertFalse(show_adopt_form(failed, {"status": "running", "trigger": "adopt"}))
        self.assertFalse(show_adopt_form(failed, {"status": "success", "trigger": "adopt"}))
        self.assertFalse(
            show_adopt_form(
                {"kind": "laravel", "managed": True, "id": "other-app", "source": "github"},
                {"status": "success", "trigger": "create"},
            )
        )
        self.assertTrue(show_adopt_form({"kind": "laravel", "managed": False, "id": "unswitched-demo"}, None))

    def test_github_link_does_not_hide_switch_on_discovered_app(self):
        from app.services.pipeline_svc import show_adopt_form

        discovered = {
            "kind": "laravel",
            "managed": True,
            "discovered": True,
            "source": "github",
            "id": "discovered-demo",
        }
        self.assertTrue(show_adopt_form(discovered, {"status": "success", "trigger": "push"}))
        self.assertTrue(show_adopt_form(discovered, None))
        self.assertFalse(show_adopt_form(discovered, {"status": "success", "trigger": "adopt"}))
        self.assertFalse(show_adopt_form(discovered, {"status": "running", "trigger": "adopt"}))

    def test_push_skips_backup_even_if_pending_adopt_left_over(self):
        from app.services.pipeline_svc import _new_pipeline

        app = {
            "id": "demo-app",
            "pending_adopt": {"migrate_data": True},
            "addons": {"database": "mysql"},
            "run_migrations": True,
            "detect": {"has_frontend": True},
        }
        push = {s["id"]: s for s in _new_pipeline(app, "push", {"sha": "abc"}).get("steps")}
        self.assertEqual(push["backup"]["status"], "skipped")
        self.assertEqual(push["data"]["status"], "skipped")
        adopt = {s["id"]: s for s in _new_pipeline(app, "adopt", {"sha": "abc"}).get("steps")}
        self.assertEqual(adopt["backup"]["status"], "pending")
        self.assertEqual(adopt["data"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
