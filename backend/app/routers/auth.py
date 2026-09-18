from __future__ import annotations

import base64
import io
from html import escape

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import AuthUser, CurrentUser, client_ip, require_action, require_priv
from app.config import Settings, get_settings
from app.services import auth_svc, mail_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _is_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def _set_session_cookie(
    response: Response, token: str, settings: Settings, request: Request
) -> None:
    secure = _is_https(request)
    response.set_cookie(
        key=auth_svc.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=secure,
        samesite="strict" if secure else "lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def _clear_cookie(response: Response, name: str, request: Request) -> None:
    secure = _is_https(request)
    response.delete_cookie(
        key=name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict" if secure else "lax",
    )


def _me_payload(user: dict) -> dict:
    settings = get_settings()
    totp_enabled = bool(user.get("totp_enabled"))
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user.get("email") or "",
        "totp_enabled": totp_enabled,
        "require_2fa": settings.require_2fa,
        "needs_2fa_setup": settings.require_2fa and not totp_enabled,
        "is_owner": bool(user.get("is_owner")),
        "privileges": auth_svc.effective_privileges(user),
        "session_ttl_hours": settings.session_ttl_hours,
    }


def _password_reset_available() -> bool:
    return any(a.get("password_encrypted") for a in mail_svc.list_accounts_raw())


def _assert_not_owner_target(target: dict) -> None:
    if target.get("is_owner"):
        raise HTTPException(403, "The owner account is managed from its own Security page")


class LoginBody(BaseModel):
    username: str
    password: str


class TotpBody(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class PasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12)


class EmailBody(BaseModel):
    email: str = ""


class ForgotBody(BaseModel):
    username: str = ""


class ResetBody(BaseModel):
    token: str
    new_password: str = Field(min_length=12)


class UserCreateBody(BaseModel):
    username: str
    password: str = Field(min_length=12)
    email: str = ""
    privileges: list[str] = Field(default_factory=list)


class UserPatchBody(BaseModel):
    email: str | None = None
    privileges: list[str] | None = None
    password: str | None = Field(default=None, min_length=12)


class SetupBody(BaseModel):
    username: str = "admin"
    email: str
    password: str = Field(min_length=12)


@router.get("/status")
def status():
    data = auth_svc.public_auth_status()
    data["password_reset_available"] = (
        False if data.get("needs_setup") else _password_reset_available()
    )
    return data


@router.post("/setup")
def setup(body: SetupBody, request: Request, response: Response):
    settings = get_settings()
    ip = client_ip(request)
    if auth_svc.is_locked_out(ip):
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in {settings.lockout_minutes} minutes.",
        )
    if not auth_svc.needs_setup():
        raise HTTPException(400, "This panel is already set up. Sign in instead.")
    try:
        rec = auth_svc.create_owner(
            username=body.username,
            email=body.email,
            password=body.password,
        )
    except ValueError as e:
        auth_svc.record_failure(ip)
        raise HTTPException(400, str(e)) from e

    auth_svc.clear_failures(ip)
    token = auth_svc.create_session_token(rec["username"])
    _set_session_cookie(response, token, settings, request)
    return {
        "ok": True,
        "needs_2fa_setup": settings.require_2fa,
        "username": rec["username"],
        "email": rec["email"],
    }


@router.get("/me")
def me(user: AuthUser):
    return _me_payload(user.record())


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    settings = get_settings()
    if auth_svc.needs_setup():
        raise HTTPException(400, "Create the first account on this panel first")
    ip = client_ip(request)
    if auth_svc.is_locked_out(ip):
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in {settings.lockout_minutes} minutes.",
        )

    rec = auth_svc.get_user_by_username(body.username)
    pass_ok = bool(rec) and auth_svc.verify_password(body.password, rec["password_hash"])
    if not rec or not pass_ok:
        auth_svc.record_failure(ip)
        raise HTTPException(401, "Invalid username or password")

    auth_svc.clear_failures(ip)
    secure = _is_https(request)

    if rec.get("totp_enabled"):
        pending = auth_svc.create_pending_token(rec["username"])
        response.set_cookie(
            key=auth_svc.PENDING_COOKIE,
            value=pending,
            httponly=True,
            secure=secure,
            samesite="strict" if secure else "lax",
            max_age=300,
            path="/",
        )
        return {"ok": True, "needs_2fa": True, "needs_2fa_setup": False}

    token = auth_svc.create_session_token(rec["username"])
    _set_session_cookie(response, token, settings, request)
    needs_setup = settings.require_2fa and not rec.get("totp_enabled")
    return {"ok": True, "needs_2fa": False, "needs_2fa_setup": needs_setup}


@router.post("/verify-2fa")
def verify_2fa(body: TotpBody, request: Request, response: Response):
    settings = get_settings()
    ip = client_ip(request)
    if auth_svc.is_locked_out(ip):
        raise HTTPException(429, "Too many failed attempts. Try again later.")

    pending = request.cookies.get(auth_svc.PENDING_COOKIE)
    payload = auth_svc.decode_pending_token(pending) if pending else None
    if not payload:
        raise HTTPException(401, "Login again — 2FA session expired")

    rec = auth_svc.get_user_by_username(payload["u"])
    if not rec or not rec.get("totp_enabled"):
        raise HTTPException(401, "Invalid 2FA state")

    if not auth_svc.verify_totp(rec["totp_secret"], body.code):
        auth_svc.record_failure(ip)
        raise HTTPException(401, "Invalid authenticator code")

    auth_svc.clear_failures(ip)
    _clear_cookie(response, auth_svc.PENDING_COOKIE, request)
    token = auth_svc.create_session_token(rec["username"])
    _set_session_cookie(response, token, settings, request)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    _clear_cookie(response, auth_svc.COOKIE_NAME, request)
    _clear_cookie(response, auth_svc.PENDING_COOKIE, request)
    return {"ok": True}


@router.get("/2fa/setup")
def setup_2fa(user: AuthUser):
    rec = user.record()
    if rec.get("totp_enabled"):
        raise HTTPException(400, "2FA already enabled")

    secret = auth_svc.ensure_totp_secret(rec)
    uri = auth_svc.totp_uri(rec["username"], secret)

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {
        "secret": secret,
        "otpauth_url": uri,
        "qr_png_base64": b64,
    }


@router.post("/2fa/enable")
def enable_2fa(body: TotpBody, user: AuthUser):
    rec = user.record()
    secret = rec.get("totp_secret")
    if not secret:
        raise HTTPException(400, "Call /2fa/setup first")
    if not auth_svc.verify_totp(secret, body.code):
        raise HTTPException(401, "Invalid authenticator code")
    rec["totp_enabled"] = True
    auth_svc.update_user(rec)
    return {"ok": True, "totp_enabled": True}


@router.post("/2fa/disable")
def disable_2fa(body: TotpBody, user: AuthUser):
    settings = get_settings()
    if settings.require_2fa:
        raise HTTPException(400, "2FA is required on this panel and cannot be disabled")
    rec = user.record()
    if not rec.get("totp_enabled"):
        return {"ok": True, "totp_enabled": False}
    if not auth_svc.verify_totp(rec.get("totp_secret") or "", body.code):
        raise HTTPException(401, "Invalid authenticator code")
    rec["totp_enabled"] = False
    rec["totp_secret"] = None
    auth_svc.update_user(rec)
    return {"ok": True, "totp_enabled": False}


@router.post("/change-password")
def change_password(body: PasswordBody, user: AuthUser):
    rec = user.record()
    if not auth_svc.verify_password(body.current_password, rec["password_hash"]):
        raise HTTPException(401, "Current password is wrong")
    rec["password_hash"] = auth_svc.hash_password(body.new_password)
    auth_svc.update_user(rec)
    return {"ok": True}


@router.post("/email")
def update_email(body: EmailBody, user: AuthUser):
    rec = user.record()
    try:
        email = auth_svc.validate_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if email and auth_svc.email_taken(email, exclude_id=rec["id"]):
        raise HTTPException(400, "That email is already in use")
    rec["email"] = email
    auth_svc.update_user(rec)
    return {"ok": True, "email": email}


@router.post("/forgot-password")
async def forgot_password(body: ForgotBody, request: Request):
    # Always ok — do not reveal whether the account exists.
    generic = {"ok": True}
    rec = auth_svc.find_for_reset(body.username)
    if not rec or not rec.get("email"):
        return generic
    accounts = [a for a in mail_svc.list_accounts_raw() if a.get("password_encrypted")]
    if not accounts:
        return generic
    token = auth_svc.create_reset_token(rec)
    url = f"{auth_svc.panel_public_url(request)}/reset-password?token={token}"
    subject = "Reset your VPS panel password"
    text = (
        f"A password reset was requested for {rec['username']}.\n\n"
        f"Open this link within 30 minutes:\n{url}\n\n"
        "If you did not ask for this, you can ignore the email.\n"
    )
    html = (
        f"<p>A password reset was requested for <strong>{escape(rec['username'])}</strong>.</p>"
        f"<p><a href=\"{escape(url)}\">Reset your password</a> (valid for 30 minutes).</p>"
        "<p>If you did not ask for this, you can ignore the email.</p>"
    )
    try:
        await mail_svc.send_message(accounts[0], subject, text, html, to_email=rec["email"])
    except Exception:
        return generic
    return generic


@router.post("/reset-password")
def reset_password(body: ResetBody):
    payload = auth_svc.decode_reset_token(body.token)
    if not payload or not payload.get("id"):
        raise HTTPException(400, "This reset link is invalid or has expired")
    rec = auth_svc.get_user_by_id(payload["id"])
    if not rec:
        raise HTTPException(400, "This reset link is invalid or has expired")
    if (rec.get("email") or "") != (payload.get("e") or ""):
        raise HTTPException(400, "This reset link is invalid or has expired")
    rec["password_hash"] = auth_svc.hash_password(body.new_password)
    auth_svc.update_user(rec)
    return {"ok": True}


@router.get("/users")
def list_users(_actor: CurrentUser = Depends(require_priv("users"))):
    return {
        "users": [auth_svc.public_user(u) for u in auth_svc.list_users()],
        "privileges": list(auth_svc.ALL_PRIVILEGES),
        "catalog": auth_svc.privilege_catalog(),
    }


@router.post("/users")
def create_user(body: UserCreateBody, actor: CurrentUser = Depends(require_action("users", "create"))):
    try:
        created = auth_svc.add_user(
            username=body.username,
            password=body.password,
            email=body.email,
            privileges=body.privileges,
            actor=actor.record(),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return auth_svc.public_user(created)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str, body: UserPatchBody, actor: CurrentUser = Depends(require_action("users", "update"))
):
    target = auth_svc.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    _assert_not_owner_target(target)
    actor_rec = actor.record()

    if body.email is not None:
        try:
            email = auth_svc.validate_email(body.email)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if email and auth_svc.email_taken(email, exclude_id=target["id"]):
            raise HTTPException(400, "That email is already in use")
        target["email"] = email

    if body.privileges is not None:
        target["privileges"] = auth_svc.apply_privilege_update(
            target, body.privileges, actor_rec
        )

    if body.password is not None:
        target["password_hash"] = auth_svc.hash_password(body.password)

    auth_svc.update_user(target)
    return auth_svc.public_user(target)


@router.post("/users/{user_id}/reset-2fa")
def reset_user_2fa(user_id: str, _actor: CurrentUser = Depends(require_action("users", "reset_2fa"))):
    target = auth_svc.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    _assert_not_owner_target(target)
    target["totp_enabled"] = False
    target["totp_secret"] = None
    auth_svc.update_user(target)
    return auth_svc.public_user(target)


@router.delete("/users/{user_id}")
def delete_user(user_id: str, actor: CurrentUser = Depends(require_action("users", "delete"))):
    target = auth_svc.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    _assert_not_owner_target(target)
    if target["id"] == actor.id:
        raise HTTPException(400, "You cannot delete your own account")
    try:
        auth_svc.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}
