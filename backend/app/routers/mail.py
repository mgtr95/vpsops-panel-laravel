from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import AuthUser, require_action, require_priv
from app.services import mail_svc, recipients_svc

router = APIRouter(
    prefix="/api/mail",
    tags=["mail"],
    dependencies=[Depends(require_priv("mail"))],
)


class MailTestBody(BaseModel):
    id: str | None = None
    name: str = "Test"
    provider: str
    host: str = ""
    port: int | None = None
    encryption: str = "starttls"
    username: str = ""
    password: str = ""
    from_email: str
    from_name: str = "VPS Backups"


class RecipientBody(BaseModel):
    id: str | None = None
    name: str
    email: str
    include_details: bool = True


class MailAccountBody(BaseModel):
    id: str | None = None
    name: str
    provider: str
    host: str = ""
    port: int | None = None
    encryption: str = "starttls"
    username: str = ""
    password: str = ""
    from_email: str
    from_name: str = "VPS Backups"
    tested: bool = False


@router.get("/recipients")
def list_recipients(_user: AuthUser):
    return recipients_svc.list_recipients()


@router.post("/recipients", dependencies=[Depends(require_action("mail", "save"))])
def upsert_recipient(body: RecipientBody, _user: AuthUser):
    try:
        return recipients_svc.upsert_recipient(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/recipients/{recipient_id}", dependencies=[Depends(require_action("mail", "delete"))])
def delete_recipient(recipient_id: str, _user: AuthUser):
    if not recipients_svc.delete_recipient(recipient_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}


@router.get("/providers")
def providers(_user: AuthUser):
    return mail_svc.list_providers()


@router.get("/accounts")
def list_accounts(_user: AuthUser):
    return mail_svc.list_accounts()


@router.post("/accounts", dependencies=[Depends(require_action("mail", "save"))])
def upsert(body: MailAccountBody, _user: AuthUser):
    try:
        return mail_svc.upsert_account(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/accounts/{account_id}", dependencies=[Depends(require_action("mail", "delete"))])
def delete(account_id: str, _user: AuthUser):
    if not mail_svc.delete_account(account_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}


@router.post("/test", dependencies=[Depends(require_action("mail", "test"))])
async def test_unsaved(body: MailTestBody, _user: AuthUser):
    try:
        return await mail_svc.send_test_for_body(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/accounts/{account_id}/test", dependencies=[Depends(require_action("mail", "test"))])
async def test_saved(account_id: str, _user: AuthUser):
    try:
        return await mail_svc.send_test(account_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
