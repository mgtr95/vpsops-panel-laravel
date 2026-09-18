from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import AuthUser, require_action, require_priv
from app.services import cert_alerts_svc, certs_svc, mail_svc, recipients_svc

router = APIRouter(
    prefix="/api/certs",
    tags=["certs"],
    dependencies=[Depends(require_priv("sites"))],
)


@router.get("")
def list_certs(_user: AuthUser):
    return certs_svc.list_certificates()


@router.get("/check")
def check(_user: AuthUser):
    return certs_svc.check_served()


@router.post("/renew", dependencies=[Depends(require_action("sites", "renew"))])
async def renew(_user: AuthUser):
    return await certs_svc.renew_certificates()


@router.post("/reload-nginx", dependencies=[Depends(require_action("sites", "reload"))])
async def reload(_user: AuthUser):
    return await certs_svc.reload_nginx()


class CertAlertsBody(BaseModel):
    enabled: bool = False
    notify: dict = {}
    events: dict = {}
    thresholds_days: list[int] = [30, 14, 7, 1]
    cooldown_minutes: int = 1440


@router.get("/alerts")
def get_alerts(_user: AuthUser):
    config = cert_alerts_svc.get_config()
    state = cert_alerts_svc.load_state()
    return {
        "config": config,
        "mail_accounts": mail_svc.list_accounts(),
        "recipients": recipients_svc.list_recipients(),
        "last_check": state.get("last_check"),
    }


@router.put("/alerts")
def save_alerts(body: CertAlertsBody, _user: AuthUser):
    try:
        config = cert_alerts_svc.save_config(body.model_dump())
        return {"ok": True, "config": config}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/alerts/test")
async def test_alerts(_user: AuthUser):
    try:
        await cert_alerts_svc.send_test_alert()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
