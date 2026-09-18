from functools import lru_cache
from pathlib import Path
import secrets

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRETS = {
    "",
    "change-me-session-secret-please",
    "replace-with-openssl-rand-hex-32",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Session signing — leave unset and one is created in the data volume on first start.
    session_secret: str = ""

    # Public HTTPS hostname (nginx). Empty = cookie Secure flag off (dev only)
    panel_domain: str = ""

    session_ttl_hours: int = 12
    max_login_attempts: int = 5
    lockout_minutes: int = 15

    # Require TOTP after password (high security). First login forces enrollment.
    require_2fa: bool = True

    apps_root: str = "/apps"
    # Host path of the same folder mounted at apps_root. Docker bind mounts
    # are resolved on the host, so compose/build must use this path.
    host_apps_dir: str = ""
    compose_scan_paths: str = "/apps"
    letsencrypt_path: str = "/etc/letsencrypt"
    rclone_rc_url: str = "http://rclone:5572"
    data_dir: str = "/data"
    panel_config_dir: str = "/panel-config"
    certbot_container: str = ""
    nginx_container: str = ""
    nginx_conf_dir: str = ""
    proxy_network: str = ""
    acme_email: str = ""
    github_app_id: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    github_app_private_key: str = ""
    github_app_webhook_secret: str = ""
    github_app_slug: str = ""
    tz: str = "UTC"

    @model_validator(mode="after")
    def _ensure_session_secret(self):
        current = (self.session_secret or "").strip()
        if current not in _PLACEHOLDER_SECRETS:
            return self
        path = Path(self.data_dir) / "session_secret"
        if path.is_file():
            stored = path.read_text().strip()
            if stored:
                self.session_secret = stored
                return self
        generated = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.session_secret = generated
        return self

    @property
    def apps_config_path(self) -> Path:
        return Path(self.panel_config_dir) / "apps.json"

    @property
    def databases_config_path(self) -> Path:
        return Path(self.panel_config_dir) / "databases.json"

    @property
    def auth_path(self) -> Path:
        return Path(self.data_dir) / "auth.json"

    @property
    def config_path(self) -> Path:
        return Path(self.data_dir) / "config.json"

    @property
    def backups_path(self) -> Path:
        return Path(self.data_dir) / "backups.json"

    @property
    def backup_history_path(self) -> Path:
        return Path(self.data_dir) / "backup_history.json"

    @property
    def mail_accounts_path(self) -> Path:
        return Path(self.data_dir) / "mail_accounts.json"

    @property
    def recipients_path(self) -> Path:
        return Path(self.data_dir) / "recipients.json"

    @property
    def notify_contacts_path(self) -> Path:
        """Legacy path — migrated to recipients.json on first read."""
        return Path(self.data_dir) / "notify_contacts.json"

    @property
    def notify_groups_path(self) -> Path:
        return Path(self.data_dir) / "notify_groups.json"

    @property
    def notify_pending_path(self) -> Path:
        return Path(self.data_dir) / "notify_pending.json"

    @property
    def jobs_path(self) -> Path:
        return Path(self.data_dir) / "jobs"

    @property
    def github_app_path(self) -> Path:
        return Path(self.data_dir) / "github_app.json"

    @property
    def github_install_path(self) -> Path:
        return Path(self.data_dir) / "github.json"

    @property
    def github_state_path(self) -> Path:
        return Path(self.data_dir) / "github_state.json"

    @property
    def pipelines_path(self) -> Path:
        return Path(self.data_dir) / "pipelines"

    @property
    def proxy_path(self) -> Path:
        return Path(self.data_dir) / "proxy.json"

    @property
    def panel_path(self) -> Path:
        return Path(self.data_dir) / "panel.json"

    @property
    def container_alerts_path(self) -> Path:
        return Path(self.data_dir) / "container_alerts.json"

    @property
    def container_alert_state_path(self) -> Path:
        return Path(self.data_dir) / "container_alert_state.json"

    @property
    def cert_alerts_path(self) -> Path:
        return Path(self.data_dir) / "cert_alerts.json"

    @property
    def cert_alert_state_path(self) -> Path:
        return Path(self.data_dir) / "cert_alert_state.json"

    def scan_paths(self) -> list[Path]:
        return [
            Path(p.strip())
            for p in self.compose_scan_paths.split(",")
            if p.strip()
        ]

    def host_path(self, panel_path: str | Path) -> str:
        """Translate a path under apps_root (/apps/...) to the host path.

        The dashboard sees apps at /apps, but `docker compose` bind mounts are
        interpreted by the host daemon. Relative volumes like `.:/var/www/app`
        must be resolved from the host directory, not /apps.
        """
        host_root = (self.host_apps_dir or "").strip().rstrip("/")
        apps_root = str(Path(self.apps_root)).rstrip("/")
        path = str(Path(panel_path))
        if not host_root:
            return path
        if path == apps_root or path.startswith(apps_root + "/"):
            return host_root + path[len(apps_root) :]
        return path

    @property
    def cookie_secure(self) -> bool:
        return bool(self.panel_domain)


@lru_cache
def get_settings() -> Settings:
    return Settings()
