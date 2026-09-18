"""OAuth helpers for the rclone setup wizard.

Client IDs/secrets are rclone's shared public apps (same as `rclone config`).
Secrets are stored rclone-obscured and revealed at runtime.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# rclone fs/config/obscure cryptKey
_OBSCURE_KEY = bytes(
    [
        0x9C,
        0x93,
        0x5B,
        0x48,
        0x73,
        0x0A,
        0x55,
        0x4D,
        0x6B,
        0xFD,
        0x7C,
        0x63,
        0xC8,
        0x86,
        0xA9,
        0x2B,
        0xD3,
        0x90,
        0x19,
        0x8E,
        0xB8,
        0x12,
        0x8A,
        0xFB,
        0xF4,
        0xDE,
        0x16,
        0x2B,
        0x8B,
        0x95,
        0xF6,
        0x38,
    ]
)

REDIRECT_127 = "http://127.0.0.1:53682/"
REDIRECT_LOCALHOST = "http://localhost:53682/"

OAUTH_OPTION_NAMES = {"config_token", "config_verification_code"}
SKIP_OPTION_NAMES = {"config_is_local"}


def reveal_obscured(value: str) -> str:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) < 16:
        raise ValueError("obscured value too short")
    iv, buf = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(_OBSCURE_KEY), modes.CTR(iv)).decryptor()
    return (decryptor.update(buf) + decryptor.finalize()).decode("utf-8")


@dataclass(frozen=True)
class OAuthApp:
    prefix: str
    label: str
    sign_in_label: str
    client_id: str
    encrypted_secret: str
    auth_url: str
    token_url: str
    redirect_uri: str
    scopes: list[str]
    extra_auth: dict[str, str]
    scope_prefix: str = ""

    def secret(self) -> str:
        return reveal_obscured(self.encrypted_secret)


# Encrypted secrets copied from rclone backend source (public shared apps).
OAUTH_APPS: dict[str, OAuthApp] = {
    "drive": OAuthApp(
        prefix="drive",
        label="Google Drive",
        sign_in_label="Sign in with Google",
        client_id="202264815644.apps.googleusercontent.com",
        encrypted_secret="eX8GpZTVx3vxMWVkuuBdDWmAUE6rGhTwVrvG9GhllYccSdj2-mvHVg",
        auth_url="https://accounts.google.com/o/oauth2/auth",
        token_url="https://oauth2.googleapis.com/token",
        redirect_uri=REDIRECT_127,
        scopes=["https://www.googleapis.com/auth/drive"],
        extra_auth={"access_type": "offline", "prompt": "consent"},
        scope_prefix="https://www.googleapis.com/auth/",
    ),
    "onedrive": OAuthApp(
        prefix="onedrive",
        label="Microsoft OneDrive",
        sign_in_label="Sign in with Microsoft",
        client_id="b15665d9-eda6-4092-8539-0eec376afd59",
        encrypted_secret="_JUdzh3LnKNqSPcf4Wu5fgMFIQOI8glZu_akYgR8yf6egowNBg-R",
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        redirect_uri=REDIRECT_LOCALHOST,
        scopes=[
            "Files.Read",
            "Files.ReadWrite",
            "Files.Read.All",
            "Files.ReadWrite.All",
            "Sites.Read.All",
            "offline_access",
        ],
        extra_auth={"response_mode": "query"},
    ),
    "dropbox": OAuthApp(
        prefix="dropbox",
        label="Dropbox",
        sign_in_label="Sign in with Dropbox",
        client_id="5jcck7diasz0rqy",
        encrypted_secret="fRS5vVLr2v6FbyXYnIgjwBuUAt0osq_QZTXAEcmZ7g",
        auth_url="https://www.dropbox.com/oauth2/authorize",
        token_url="https://api.dropboxapi.com/oauth2/token",
        redirect_uri=REDIRECT_LOCALHOST,
        scopes=[
            "files.metadata.write",
            "files.content.write",
            "files.content.read",
            "sharing.write",
            "account_info.read",
        ],
        extra_auth={"token_access_type": "offline"},
    ),
    "box": OAuthApp(
        prefix="box",
        label="Box",
        sign_in_label="Sign in with Box",
        client_id="d0374ba6pgmaguie02ge15sv1mllndho",
        encrypted_secret="sYbJYm99WB8jzeaLPU0OPDMJKIkZvD2qOn3SyEMfiJr03RdtDt3xcZEIudRhbIDL",
        auth_url="https://app.box.com/api/oauth2/authorize",
        token_url="https://app.box.com/api/oauth2/token",
        redirect_uri=REDIRECT_127,
        scopes=[],
        extra_auth={},
    ),
}


def get_app(prefix: str) -> OAuthApp | None:
    return OAUTH_APPS.get((prefix or "").strip().lower())


def _scopes(app: OAuthApp, scope: str | None) -> str:
    if scope:
        raw = scope.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if app.scope_prefix and " " not in raw and "/" not in raw:
            return f"{app.scope_prefix}{raw}"
        return raw
    return " ".join(app.scopes)


def build_auth_url(
    prefix: str,
    *,
    client_id: str | None = None,
    scope: str | None = None,
    state: str = "rclone-dash",
) -> dict[str, Any] | None:
    app = get_app(prefix)
    if not app:
        return None
    cid = (client_id or "").strip() or app.client_id
    params = {
        "client_id": cid,
        "redirect_uri": app.redirect_uri,
        "response_type": "code",
        "state": state,
        **app.extra_auth,
    }
    scopes = _scopes(app, scope)
    if scopes:
        params["scope"] = scopes
    return {
        "provider": app.prefix,
        "label": app.label,
        "sign_in_label": app.sign_in_label,
        "auth_url": f"{app.auth_url}?{urlencode(params)}",
        "mode": "token",
        "redirect_hint": app.redirect_uri,
    }


_URL_IN_HELP = re.compile(r"https?://[^\s<>\"]+")


def oauth_from_option(prefix: str, option: dict[str, Any] | None, hints: dict[str, str] | None = None) -> dict[str, Any] | None:
    hints = hints or {}
    name = (option or {}).get("Name") or ""
    if name not in OAUTH_OPTION_NAMES:
        return None
    if name == "config_verification_code":
        help_text = option.get("Help") or ""
        urls = _URL_IN_HELP.findall(help_text)
        auth_url = next((u for u in urls if "127.0.0.1" not in u and "localhost" not in u), urls[0] if urls else "")
        app = get_app(prefix)
        return {
            "provider": prefix,
            "label": app.label if app else prefix,
            "sign_in_label": app.sign_in_label if app else "Open sign-in page",
            "auth_url": auth_url,
            "mode": "code",
            "redirect_hint": "",
        }
    built = build_auth_url(
        prefix,
        client_id=hints.get("client_id"),
        scope=hints.get("scope"),
    )
    return built


def extract_code(redirect_url_or_code: str) -> str:
    text = (redirect_url_or_code or "").strip().strip('"')
    if not text:
        raise ValueError("Paste the address from your browser after signing in.")
    if "://" in text or text.startswith("http"):
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        if parsed.fragment:
            qs.update(parse_qs(parsed.fragment))
        if qs.get("error"):
            desc = (qs.get("error_description") or qs.get("error") or ["authorization failed"])[0]
            raise ValueError(desc)
        code = (qs.get("code") or [""])[0]
        if not code:
            raise ValueError("No code found in that address. Copy the full URL from the address bar.")
        return code
    if "code=" in text:
        qs = parse_qs(text.split("?", 1)[-1])
        if qs.get("code"):
            return qs["code"][0]
    return text


def _token_payload(data: dict[str, Any]) -> str:
    if data.get("error"):
        desc = data.get("error_description") or data.get("error")
        raise ValueError(str(desc))
    access = data.get("access_token")
    if not access:
        raise ValueError("Provider did not return an access token.")
    expires_in = int(data.get("expires_in") or 3600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    token = {
        "access_token": access,
        "token_type": data.get("token_type") or "Bearer",
        "refresh_token": data.get("refresh_token") or "",
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    return json.dumps(token, separators=(",", ":"))


async def exchange_code(
    prefix: str,
    redirect_url_or_code: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str | None = None,
) -> str:
    app = get_app(prefix)
    if not app:
        raise ValueError(f"No built-in sign-in helper for '{prefix}'.")
    code = extract_code(redirect_url_or_code)
    cid = (client_id or "").strip() or app.client_id
    secret = (client_secret or "").strip() or app.secret()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": app.redirect_uri,
        "client_id": cid,
        "client_secret": secret,
    }
    scopes = _scopes(app, scope)
    if prefix == "onedrive" and scopes:
        form["scope"] = scopes
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(app.token_url, data=form)
        try:
            data = resp.json()
        except Exception as exc:
            raise ValueError(resp.text or "Token exchange failed") from exc
        if resp.status_code >= 400 and not data.get("error"):
            raise ValueError(data.get("message") or resp.text or "Token exchange failed")
        return _token_payload(data)


def parse_token(token: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(token, dict):
        return token
    text = (token or "").strip()
    if not text:
        raise ValueError("OneDrive remote has no OAuth token.")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("OneDrive token is not valid JSON.")
    return data


async def _onedrive_refresh(token: dict[str, Any]) -> str:
    app = get_app("onedrive")
    if not app:
        raise ValueError("No built-in OneDrive helper.")
    refresh = (token.get("refresh_token") or "").strip()
    if not refresh:
        raise ValueError("OneDrive access token expired and there is no refresh token.")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": app.client_id,
        "client_secret": app.secret(),
        "scope": _scopes(app, None),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(app.token_url, data=form)
        try:
            data = resp.json()
        except Exception as exc:
            raise ValueError(resp.text or "OneDrive token refresh failed") from exc
        if resp.status_code >= 400 and not data.get("error"):
            raise ValueError(data.get("message") or resp.text or "OneDrive token refresh failed")
        if not data.get("refresh_token"):
            data["refresh_token"] = refresh
        return _token_payload(data)


async def onedrive_me_drive(token: str | dict[str, Any] | None) -> dict[str, Any]:
    """Look up the signed-in user's default OneDrive via Microsoft Graph.

    Returns drive id/type/name and a token JSON string (refreshed when needed).
    """
    parsed = parse_token(token)
    token_json = json.dumps(parsed, separators=(",", ":")) if not isinstance(token, str) else token

    async def _get(access: str) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/me/drive",
                headers={"Authorization": f"Bearer {access}"},
            )
            try:
                data = resp.json()
            except Exception:
                data = {"error": {"message": resp.text or "Graph request failed"}}
            return resp.status_code, data

    status, data = await _get(parsed.get("access_token") or "")
    if status == 401:
        token_json = await _onedrive_refresh(parsed)
        parsed = parse_token(token_json)
        status, data = await _get(parsed.get("access_token") or "")
    if status >= 400 or data.get("error"):
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        msg = err.get("message") or data.get("error") or f"Microsoft Graph returned HTTP {status}"
        raise ValueError(f"Could not look up the OneDrive: {msg}")
    drive_id = data.get("id") or ""
    drive_type = data.get("driveType") or ""
    if not drive_id or not drive_type:
        raise ValueError("Microsoft Graph did not return a drive id and type.")
    return {
        "id": drive_id,
        "driveType": drive_type,
        "name": data.get("name") or "",
        "token": token_json,
    }
