from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import AuthUser, require_action, require_priv
from app.services import proxy_svc

router = APIRouter(
    prefix="/api/proxy",
    tags=["proxy"],
    dependencies=[Depends(require_priv("sites"))],
)


class SetupBody(BaseModel):
    acme_email: str


@router.get("")
def proxy_status(_user: AuthUser):
    return proxy_svc.status()


@router.post("/setup", dependencies=[Depends(require_action("sites", "setup"))])
def proxy_setup(body: SetupBody, _user: AuthUser):
    try:
        return proxy_svc.setup(body.acme_email)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e
