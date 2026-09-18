from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth import OwnerUser
from app.services import panel_access_svc

router = APIRouter(prefix="/api", tags=["access"])


class DomainBody(BaseModel):
    domain: str
    www: bool = True


@router.get("/access")
def access_info():
    """How this panel is meant to be reached (no secrets)."""
    return panel_access_svc.access_info()


@router.post("/access/check")
def check_domain(body: DomainBody, _user: OwnerUser):
    try:
        return panel_access_svc.check_panel_dns(body.domain, include_www=body.www)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/access/apply")
async def apply_domain(body: DomainBody, _user: OwnerUser):
    try:
        return await panel_access_svc.apply_domain(body.domain, include_www=body.www)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/access/domain")
async def clear_domain(_user: OwnerUser):
    try:
        return await panel_access_svc.clear_domain()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
