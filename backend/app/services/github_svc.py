"""GitHub App connect: manifest, install, tokens, repos, webhook, commit status."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import get_settings
from app.crypto import decrypt_text, encrypt_text
from app.utils import read_json, write_json

GITHUB_API = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"


def public_base_url(request) -> str:
    from app.services.panel_access_svc import effective_panel_domain

    domain = (effective_panel_domain() or "").strip()
    if domain:
        proto = "https"
        forwarded = ""
        if request is not None:
            forwarded = (request.headers.get("x-forwarded-proto") or "").lower()
            if forwarded:
                proto = forwarded.split(",")[0].strip()
            elif request.url.scheme:
                proto = request.url.scheme
        return f"{proto}://{domain}".rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:9090"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encrypt_fields(data: dict, keys: tuple[str, ...]) -> dict:
    out = dict(data)
    for key in keys:
        val = out.get(key)
        if val and not str(val).startswith("enc:"):
            out[key] = "enc:" + encrypt_text(val)
    return out


def _decrypt_fields(data: dict, keys: tuple[str, ...]) -> dict:
    out = dict(data)
    for key in keys:
        val = out.get(key) or ""
        if isinstance(val, str) and val.startswith("enc:"):
            out[key] = decrypt_text(val[4:])
    return out


SECRET_APP_KEYS = ("client_secret", "webhook_secret", "pem")


def load_app() -> dict | None:
    settings = get_settings()
    stored = read_json(settings.github_app_path, None)
    if stored:
        return _decrypt_fields(stored, SECRET_APP_KEYS)
    if settings.github_app_id and settings.github_app_private_key:
        pem = settings.github_app_private_key.replace("\\n", "\n")
        return {
            "id": int(settings.github_app_id) if str(settings.github_app_id).isdigit() else settings.github_app_id,
            "client_id": settings.github_app_client_id,
            "client_secret": settings.github_app_client_secret,
            "webhook_secret": settings.github_app_webhook_secret,
            "pem": pem,
            "slug": settings.github_app_slug,
            "name": settings.github_app_slug or "VPS Dashboard",
            "source": "env",
        }
    return None


def save_app(data: dict) -> None:
    settings = get_settings()
    write_json(settings.github_app_path, _encrypt_fields(dict(data), SECRET_APP_KEYS))


def clear_app() -> None:
    settings = get_settings()
    for path in (settings.github_app_path, settings.github_install_path, settings.github_state_path):
        if path.exists():
            path.unlink()


def load_install() -> dict | None:
    settings = get_settings()
    data = read_json(settings.github_install_path, None)
    return data if data and data.get("installation_id") else None


def save_install(data: dict) -> None:
    settings = get_settings()
    write_json(settings.github_install_path, data)


def clear_install() -> None:
    settings = get_settings()
    if settings.github_install_path.exists():
        settings.github_install_path.unlink()


def new_state(purpose: str, extra: dict | None = None) -> str:
    settings = get_settings()
    token = secrets.token_urlsafe(24)
    payload = {
        "state": token,
        "purpose": purpose,
        "created_at": _now(),
        **(extra or {}),
    }
    write_json(settings.github_state_path, payload)
    return token


def pop_state(state: str, purpose: str | None = None) -> dict | None:
    settings = get_settings()
    stored = read_json(settings.github_state_path, None)
    if not stored or stored.get("state") != state:
        return None
    created = stored.get("created_at")
    try:
        ts = datetime.fromisoformat(created)
        if (datetime.now(timezone.utc) - ts).total_seconds() > 900:
            return None
    except Exception:
        return None
    if purpose and stored.get("purpose") != purpose:
        return None
    if settings.github_state_path.exists():
        settings.github_state_path.unlink()
    return stored


def build_manifest(base_url: str) -> dict:
    from app.services.panel_access_svc import effective_panel_domain

    host = (effective_panel_domain() or "").strip() or "vps"
    suffix = secrets.token_hex(2)
    name = f"VPS Dash {host}"[:50]
    if len(name) < 8:
        name = f"VPS Dashboard {suffix}"
    base = base_url.rstrip("/")
    return {
        "name": name,
        "url": base,
        "hook_attributes": {"url": f"{base}/api/github/webhook", "active": True},
        "redirect_url": f"{base}/api/github/manifest/callback",
        "callback_urls": [f"{base}/api/github/callback"],
        "setup_url": f"{base}/api/github/setup",
        "public": False,
        "default_permissions": {
            "contents": "read",
            "metadata": "read",
            "statuses": "write",
            "checks": "write",
        },
        "default_events": ["push"],
    }


def status_payload() -> dict:
    app = load_app()
    install = load_install()
    return {
        "configured": bool(app),
        "connected": bool(app and install),
        "app_name": (app or {}).get("name"),
        "app_slug": (app or {}).get("slug"),
        "account": (install or {}).get("account"),
        "installation_id": (install or {}).get("installation_id"),
        "html_url": (
            f"https://github.com/settings/installations/{install['installation_id']}"
            if install
            else None
        ),
        "public_url": (app or {}).get("public_url"),
    }


def app_jwt(app: dict | None = None) -> str:
    app = app or load_app()
    if not app or not app.get("pem"):
        raise RuntimeError("GitHub App is not configured")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": int(app["id"])}
    return jwt.encode(payload, app["pem"], algorithm="RS256")


async def convert_manifest(code: str, public_url: str) -> dict:
    url = f"{GITHUB_API}/app-manifests/{code}/conversions"
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(url, headers={"Accept": GITHUB_ACCEPT, "X-GitHub-Api-Version": "2022-11-28"})
        res.raise_for_status()
        raw = res.json()
    data = {
        "id": raw["id"],
        "slug": raw.get("slug"),
        "name": raw.get("name"),
        "client_id": raw.get("client_id"),
        "client_secret": raw.get("client_secret"),
        "webhook_secret": raw.get("webhook_secret"),
        "pem": raw.get("pem"),
        "html_url": raw.get("html_url"),
        "public_url": public_url,
        "created_at": _now(),
    }
    save_app(data)
    return data


async def installation_token(installation_id: int | None = None) -> str:
    app = load_app()
    if not app:
        raise RuntimeError("GitHub App is not configured")
    if installation_id is None:
        inst = load_install()
        if not inst:
            raise RuntimeError("GitHub is not connected")
        installation_id = int(inst["installation_id"])
    token = app_jwt(app)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers=_app_headers(token),
        )
        res.raise_for_status()
        return res.json()["token"]


def _app_headers(jwt_token: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": f"Bearer {jwt_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _token_headers(token: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_installation(installation_id: int) -> dict:
    app = load_app()
    token = app_jwt(app)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GITHUB_API}/app/installations/{installation_id}",
            headers=_app_headers(token),
        )
        res.raise_for_status()
        raw = res.json()
    account = (raw.get("account") or {}).get("login")
    data = {
        "installation_id": raw["id"],
        "account": account,
        "account_type": (raw.get("account") or {}).get("type"),
        "repository_selection": raw.get("repository_selection"),
        "html_url": raw.get("html_url"),
        "updated_at": _now(),
    }
    save_install(data)
    return data


async def list_repos() -> list[dict]:
    token = await installation_token()
    repos: list[dict] = []
    url: str | None = f"{GITHUB_API}/installation/repositories?per_page=100"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            res = await client.get(url, headers=_token_headers(token))
            res.raise_for_status()
            payload = res.json()
            for repo in payload.get("repositories") or []:
                repos.append(
                    {
                        "id": repo["id"],
                        "full_name": repo["full_name"],
                        "name": repo["name"],
                        "owner": (repo.get("owner") or {}).get("login"),
                        "private": repo.get("private"),
                        "default_branch": repo.get("default_branch") or "main",
                        "description": repo.get("description") or "",
                        "html_url": repo.get("html_url"),
                    }
                )
            url = _next_link(res.headers.get("link"))
    return repos


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


async def get_repo(owner: str, repo: str) -> dict:
    token = await installation_token()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_token_headers(token))
        res.raise_for_status()
        return res.json()


async def list_branches(owner: str, repo: str) -> list[str]:
    token = await installation_token()
    names: list[str] = []
    url: str | None = f"{GITHUB_API}/repos/{owner}/{repo}/branches?per_page=100"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            res = await client.get(url, headers=_token_headers(token))
            res.raise_for_status()
            names.extend(b["name"] for b in res.json())
            url = _next_link(res.headers.get("link"))
    return names


async def get_file(owner: str, repo: str, path: str, ref: str) -> str | None:
    token = await installation_token()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_token_headers(token),
            params={"ref": ref},
        )
        if res.status_code == 404:
            return None
        res.raise_for_status()
        data = res.json()
    if data.get("encoding") == "base64" and data.get("content"):
        import base64

        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return None


async def latest_commit(owner: str, repo: str, branch: str) -> dict:
    token = await installation_token()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{branch}",
            headers=_token_headers(token),
        )
        res.raise_for_status()
        raw = res.json()
    commit = raw.get("commit") or {}
    author = (commit.get("author") or {})
    return {
        "sha": raw.get("sha"),
        "message": (commit.get("message") or "").split("\n", 1)[0],
        "author": author.get("name") or ((raw.get("author") or {}).get("login")),
        "date": author.get("date"),
        "html_url": raw.get("html_url"),
    }


async def set_commit_status(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str,
    target_url: str | None = None,
) -> None:
    if not sha:
        return
    try:
        token = await installation_token()
        body: dict[str, Any] = {
            "state": state,
            "description": description[:140],
            "context": "vps-dashboard/deploy",
        }
        if target_url:
            body["target_url"] = target_url
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{sha}",
                headers=_token_headers(token),
                json=body,
            )
    except Exception:
        return


def verify_webhook(body: bytes, signature: str) -> bool:
    app = load_app()
    secret = (app or {}).get("webhook_secret") or ""
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


def install_url(state: str) -> str:
    app = load_app()
    if not app or not app.get("slug"):
        raise RuntimeError("GitHub App is not configured")
    return f"https://github.com/apps/{app['slug']}/installations/new?{urlencode({'state': state})}"


_SECRET_RE = (
    re.compile(r"(?i)(AUTHORIZATION:\s*(?:bearer|basic)\s+)\S+"),
    re.compile(r"(?i)(x-access-token:)[^@\s]+"),
    re.compile(r"(?i)\bghs_[A-Za-z0-9._-]+"),
)


def redact_secrets(text: str) -> str:
    out = text or ""
    for pat in _SECRET_RE:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "ghs_") + "***", out)
    return out


def git_safe_args(cwd: str | None = None) -> list[str]:
    """Mark host-mounted app dirs safe. The panel runs as root; repos are often owned by the host user."""
    args = ["-c", "safe.directory=*"]
    if cwd:
        args.extend(["-c", f"safe.directory={Path(cwd).as_posix()}"])
    return args


def git_config_args(token: str, cwd: str | None = None) -> list[str]:
    """Git -c flags so fetch/checkout uses the GitHub App token over HTTPS.

    Maps both HTTPS and SSH remotes (git@github.com: / ssh://git@github.com/).
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    instead = f"https://x-access-token:{token}@github.com/"
    return [
        *git_safe_args(cwd),
        "-c",
        "credential.helper=",
        "-c",
        f"http.extraheader=AUTHORIZATION: basic {basic}",
        "-c",
        f"url.{instead}.insteadOf=https://github.com/",
        "-c",
        f"url.{instead}.insteadOf=git@github.com:",
        "-c",
        f"url.{instead}.insteadOf=ssh://git@github.com/",
    ]


async def repo_accessible(owner: str, repo: str) -> bool:
    full_name = f"{owner}/{repo}"
    try:
        repos = await list_repos()
    except Exception:
        return False
    return any(r.get("full_name") == full_name for r in repos)
