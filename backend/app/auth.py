from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status

from app.services import auth_svc


@dataclass
class CurrentUser:
    id: str
    username: str
    email: str = ""
    totp_enabled: bool = False
    is_owner: bool = False
    privileges: list[str] = field(default_factory=list)

    def has(self, priv: str) -> bool:
        if self.is_owner:
            return True
        return priv in self.privileges

    def can(self, module: str, action: str) -> bool:
        if self.is_owner:
            return True
        return f"{module}.{action}" in self.privileges

    def record(self) -> dict:
        rec = auth_svc.get_user_by_id(self.id)
        if not rec:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired",
            )
        return rec


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _from_record(rec: dict) -> CurrentUser:
    return CurrentUser(
        id=rec["id"],
        username=rec["username"],
        email=rec.get("email") or "",
        totp_enabled=bool(rec.get("totp_enabled")),
        is_owner=bool(rec.get("is_owner")),
        privileges=auth_svc.effective_privileges(rec),
    )


def require_auth(request: Request) -> CurrentUser:
    token = request.cookies.get(auth_svc.COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = auth_svc.decode_session_token(token)
    if not payload or not payload.get("u"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
        )
    rec = auth_svc.get_user_by_username(payload["u"])
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
        )
    return _from_record(rec)


def require_priv(priv: str) -> Callable[[Request], CurrentUser]:
    def dep(request: Request) -> CurrentUser:
        user = require_auth(request)
        if not user.has(priv):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this part of the panel",
            )
        return user

    return dep


def require_action(module: str, action: str) -> Callable[[Request], CurrentUser]:
    def dep(request: Request) -> CurrentUser:
        user = require_auth(request)
        if not user.has(module):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this part of the panel",
            )
        if not user.can(module, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to do this",
            )
        return user

    return dep


def assert_can(user: CurrentUser, module: str, action: str) -> None:
    if not user.can(module, action):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to do this",
        )


def require_owner(request: Request) -> CurrentUser:
    user = require_auth(request)
    if not user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can change this",
        )
    return user


AuthUser = Annotated[CurrentUser, Depends(require_auth)]
OwnerUser = Annotated[CurrentUser, Depends(require_owner)]
