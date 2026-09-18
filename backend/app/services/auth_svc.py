from __future__ import annotations

import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

import bcrypt
import pyotp
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.utils import read_json, write_json

COOKIE_NAME = "vps_panel_session"
PENDING_COOKIE = "vps_panel_pending"

ALL_PRIVILEGES = (
    "docker",
    "apps",
    "sites",
    "db",
    "rclone",
    "backups",
    "mail",
    "users",
)

CURRENT_PRIV_VERSION = 2

PRIVILEGE_LABELS = {
    "docker": "Docker",
    "apps": "Apps",
    "sites": "Websites",
    "db": "Databases",
    "rclone": "Rclone",
    "backups": "Backups",
    "mail": "Mail",
    "users": "Manage users",
}

PRIVILEGE_ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "docker": (
        ("start", "Start"),
        ("stop", "Stop"),
        ("restart", "Restart"),
        ("remove", "Remove"),
        ("logs", "Logs"),
        ("inspect", "Inspect"),
        ("prune", "Prune"),
    ),
    "apps": (
        ("create", "Create"),
        ("deploy", "Deploy"),
        ("restart", "Restart"),
        ("domain", "Domain / HTTPS"),
        ("delete", "Remove"),
        ("github", "GitHub"),
    ),
    "sites": (
        ("setup", "Start hosting"),
        ("renew", "Renew certificates"),
        ("reload", "Reload nginx"),
    ),
    "db": (
        ("insert", "Add rows"),
        ("update", "Edit rows"),
        ("delete_row", "Delete rows"),
        ("sql", "Run SQL"),
        ("dump", "Download dump"),
        ("import", "Import"),
    ),
    "rclone": (
        ("add_remote", "Add storage"),
        ("delete_remote", "Remove storage"),
        ("mkdir", "New folder"),
        ("delete", "Delete files"),
        ("transfer", "Copy / sync / move"),
        ("download_config", "Download rclone.conf"),
        ("raw_rc", "Raw rclone API"),
    ),
    "backups": (
        ("save", "Save jobs"),
        ("delete", "Delete jobs"),
        ("run", "Run now"),
    ),
    "mail": (
        ("save", "Save accounts"),
        ("delete", "Delete accounts"),
        ("test", "Send test"),
    ),
    "users": (
        ("create", "Add users"),
        ("update", "Edit users"),
        ("delete", "Delete users"),
        ("reset_2fa", "Clear 2FA"),
    ),
}


def all_privilege_keys() -> list[str]:
    keys: list[str] = []
    for module in ALL_PRIVILEGES:
        keys.append(module)
        for action, _label in PRIVILEGE_ACTIONS.get(module, ()):
            keys.append(f"{module}.{action}")
    return keys


KNOWN_PRIVILEGES = frozenset(all_privilege_keys())


def privilege_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": module,
            "label": PRIVILEGE_LABELS[module],
            "actions": [
                {"id": action, "label": label}
                for action, label in PRIVILEGE_ACTIONS.get(module, ())
            ],
        }
        for module in ALL_PRIVILEGES
    ]


def _module_action_keys(module: str) -> list[str]:
    return [f"{module}.{action}" for action, _label in PRIVILEGE_ACTIONS.get(module, ())]


def expand_legacy_privileges(requested: list[str] | None) -> list[str]:
    """Module-only lists (v1) become module + every action for that module."""
    seen = set(requested or [])
    ordered: list[str] = []
    for module in ALL_PRIVILEGES:
        if module not in seen:
            continue
        ordered.append(module)
        ordered.extend(_module_action_keys(module))
    return ordered


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    settings = settings or get_settings()
    return URLSafeTimedSerializer(settings.session_secret, salt="vps-panel-session-v1")


def _pending_serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    settings = settings or get_settings()
    return URLSafeTimedSerializer(settings.session_secret, salt="vps-panel-pending-v1")


def _reset_serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    settings = settings or get_settings()
    return URLSafeTimedSerializer(settings.session_secret, salt="vps-panel-reset-v1")


# --- rate limit (per IP) ---
_attempts: dict[str, list[float]] = {}


def is_locked_out(ip: str) -> bool:
    settings = get_settings()
    window = settings.lockout_minutes * 60
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < window]
    _attempts[ip] = hits
    return len(hits) >= settings.max_login_attempts


def record_failure(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _attempts.pop(ip, None)


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError("Username must be 2–32 characters: letters, numbers, dot, underscore, hyphen.")
    return name


def validate_email(value: str | None, *, required: bool = False) -> str:
    email = normalize_email(value)
    if not email:
        if required:
            raise ValueError("Email is required")
        return ""
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError("Enter a valid email address")
    return email


def sanitize_privileges(requested: list[str] | None, actor: dict | None = None) -> list[str]:
    seen = {p for p in (requested or []) if p in KNOWN_PRIVILEGES}
    ordered: list[str] = []
    for module in ALL_PRIVILEGES:
        action_keys = [key for key in _module_action_keys(module) if key in seen]
        if module not in seen and not action_keys:
            continue
        # Leftover actions (e.g. ones the actor cannot revoke) still need the module.
        ordered.append(module)
        ordered.extend(action_keys)
    if actor is None or actor.get("is_owner"):
        return ordered
    allowed = set(effective_privileges(actor))
    return sanitize_privileges([p for p in ordered if p in allowed])


def apply_privilege_update(target: dict, requested: list[str] | None, actor: dict) -> list[str]:
    """Non-owners can only change privileges they themselves have. Others are left as-is."""
    if actor.get("is_owner"):
        return sanitize_privileges(requested, actor)
    actor_privs = set(effective_privileges(actor))
    unchanged = [p for p in effective_privileges(target) if p not in actor_privs]
    updated = sanitize_privileges(requested, actor)
    return sanitize_privileges(unchanged + updated)


def effective_privileges(user: dict) -> list[str]:
    if user.get("is_owner"):
        return all_privilege_keys()
    return sanitize_privileges(user.get("privileges") or [])


def public_user(user: dict) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user.get("email") or "",
        "totp_enabled": bool(user.get("totp_enabled")),
        "is_owner": bool(user.get("is_owner")),
        "privileges": effective_privileges(user),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def _normalize_user(raw: dict, *, is_owner: bool = False) -> dict[str, Any]:
    owner = bool(is_owner or raw.get("is_owner"))
    try:
        version = int(raw.get("priv_version") or 1)
    except (TypeError, ValueError):
        version = 1
    raw_privs = list(raw.get("privileges") or [])
    if owner:
        privs = all_privilege_keys()
        version = CURRENT_PRIV_VERSION
    elif version < CURRENT_PRIV_VERSION:
        privs = expand_legacy_privileges(raw_privs)
        version = CURRENT_PRIV_VERSION
    else:
        privs = sanitize_privileges(raw_privs)
    return {
        "id": raw.get("id") or secrets.token_hex(8),
        "username": (raw.get("username") or "").strip(),
        "email": normalize_email(raw.get("email")),
        "password_hash": raw.get("password_hash") or "",
        "totp_secret": raw.get("totp_secret"),
        "totp_enabled": bool(raw.get("totp_enabled")),
        "is_owner": owner,
        "privileges": privs,
        "priv_version": version,
        "created_at": raw.get("created_at") or _now(),
        "updated_at": raw.get("updated_at") or _now(),
    }


def _migrate(data: dict) -> dict[str, Any] | None:
    if isinstance(data.get("users"), list):
        users_in = [u for u in data["users"] if isinstance(u, dict) and u.get("username")]
        if not users_in:
            return {"users": []}
        has_owner = any(u.get("is_owner") for u in users_in)
        users: list[dict] = []
        found_owner = False
        for i, raw in enumerate(users_in):
            owner = bool(raw.get("is_owner")) if has_owner else i == 0
            if found_owner:
                owner = False
            if owner:
                found_owner = True
            users.append(_normalize_user(raw, is_owner=owner))
        if not found_owner and users:
            users[0] = _normalize_user(users[0], is_owner=True)
        return {"users": users}

    if data.get("username") and data.get("password_hash"):
        return {"users": [_normalize_user(data, is_owner=True)]}
    return None


def load_store() -> dict[str, Any]:
    settings = get_settings()
    data = read_json(settings.auth_path, None)
    if isinstance(data, dict):
        migrated = _migrate(data)
        if migrated:
            if migrated != data:
                write_json(settings.auth_path, migrated)
            return migrated
    return {"users": []}


_setup_lock = threading.Lock()


def needs_setup() -> bool:
    return len(list_users()) == 0


def create_owner(*, username: str, email: str, password: str) -> dict[str, Any]:
    name = validate_username(username)
    mail = validate_email(email, required=True)
    if len(password or "") < 12:
        raise ValueError("Password must be at least 12 characters")
    now = _now()
    user = {
        "id": secrets.token_hex(8),
        "username": name,
        "email": mail,
        "password_hash": hash_password(password),
        "totp_secret": None,
        "totp_enabled": False,
        "is_owner": True,
        "privileges": all_privilege_keys(),
        "priv_version": CURRENT_PRIV_VERSION,
        "created_at": now,
        "updated_at": now,
    }
    with _setup_lock:
        if list_users():
            raise ValueError("This panel already has an owner")
        save_store({"users": [user]})
    return user


def load_auth() -> dict[str, Any]:
    """Bootstrap / migrate on startup. Prefer load_store() for new code."""
    return load_store()


def save_store(store: dict[str, Any]) -> None:
    settings = get_settings()
    write_json(settings.auth_path, store)


def list_users() -> list[dict[str, Any]]:
    return list(load_store().get("users") or [])


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    for user in list_users():
        if user.get("id") == user_id:
            return user
    return None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    needle = (username or "").strip().lower()
    if not needle:
        return None
    for user in list_users():
        if (user.get("username") or "").lower() == needle:
            return user
    return None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    needle = normalize_email(email)
    if not needle:
        return None
    for user in list_users():
        if (user.get("email") or "") == needle:
            return user
    return None


def find_for_reset(identifier: str) -> dict[str, Any] | None:
    raw = (identifier or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return get_user_by_email(raw)
    return get_user_by_username(raw)


def email_taken(email: str, *, exclude_id: str | None = None) -> bool:
    needle = normalize_email(email)
    if not needle:
        return False
    for user in list_users():
        if exclude_id and user.get("id") == exclude_id:
            continue
        if (user.get("email") or "") == needle:
            return True
    return False


def username_taken(username: str, *, exclude_id: str | None = None) -> bool:
    needle = (username or "").strip().lower()
    for user in list_users():
        if exclude_id and user.get("id") == exclude_id:
            continue
        if (user.get("username") or "").lower() == needle:
            return True
    return False


def update_user(user: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    user = dict(user)
    if user.get("is_owner"):
        user["privileges"] = all_privilege_keys()
    user["priv_version"] = CURRENT_PRIV_VERSION
    user["updated_at"] = _now()
    for i, existing in enumerate(store["users"]):
        if existing.get("id") == user.get("id"):
            store["users"][i] = user
            save_store(store)
            return user
    raise ValueError("User not found")


def add_user(
    *,
    username: str,
    password: str,
    email: str = "",
    privileges: list[str] | None = None,
    actor: dict | None = None,
) -> dict[str, Any]:
    name = validate_username(username)
    if username_taken(name):
        raise ValueError("That username is already in use")
    mail = validate_email(email)
    if mail and email_taken(mail):
        raise ValueError("That email is already in use")
    now = _now()
    user = {
        "id": secrets.token_hex(8),
        "username": name,
        "email": mail,
        "password_hash": hash_password(password),
        "totp_secret": None,
        "totp_enabled": False,
        "is_owner": False,
        "privileges": sanitize_privileges(privileges, actor),
        "priv_version": CURRENT_PRIV_VERSION,
        "created_at": now,
        "updated_at": now,
    }
    store = load_store()
    store["users"].append(user)
    save_store(store)
    return user


def delete_user(user_id: str) -> bool:
    store = load_store()
    users = store.get("users") or []
    target = next((u for u in users if u.get("id") == user_id), None)
    if not target:
        return False
    if target.get("is_owner"):
        raise ValueError("The owner account cannot be deleted")
    store["users"] = [u for u in users if u.get("id") != user_id]
    save_store(store)
    return True


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def create_session_token(username: str) -> str:
    settings = get_settings()
    return _serializer(settings).dumps(
        {"u": username, "n": secrets.token_hex(8)}
    )


def decode_session_token(token: str) -> dict | None:
    settings = get_settings()
    max_age = settings.session_ttl_hours * 3600
    try:
        return _serializer(settings).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def create_pending_token(username: str) -> str:
    settings = get_settings()
    return _pending_serializer(settings).dumps(
        {"u": username, "n": secrets.token_hex(8)}
    )


def decode_pending_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return _pending_serializer(settings).loads(token, max_age=300)
    except (BadSignature, SignatureExpired):
        return None


def create_reset_token(user: dict) -> str:
    return _reset_serializer().dumps(
        {"id": user["id"], "e": user.get("email") or ""}
    )


def decode_reset_token(token: str) -> dict | None:
    try:
        return _reset_serializer().loads(token, max_age=1800)
    except (BadSignature, SignatureExpired):
        return None


def panel_public_url(request) -> str:
    from app.services.panel_access_svc import effective_panel_domain

    domain = (effective_panel_domain() or "").strip()
    if domain:
        return f"https://{domain}".rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (
        (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}".rstrip("/")


def ensure_totp_secret(user: dict) -> str:
    if user.get("totp_secret"):
        return user["totp_secret"]
    user["totp_secret"] = pyotp.random_base32()
    update_user(user)
    return user["totp_secret"]


def totp_uri(username: str, secret: str) -> str:
    from app.services.panel_access_svc import effective_panel_domain

    issuer = effective_panel_domain() or "VPS-Dashboard"
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    if not code or not secret:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code.strip().replace(" ", ""), valid_window=1)


def public_auth_status() -> dict:
    from app.services.panel_access_svc import effective_panel_domain

    settings = get_settings()
    return {
        "require_2fa": settings.require_2fa,
        "panel_domain": effective_panel_domain() or None,
        "needs_setup": needs_setup(),
    }
