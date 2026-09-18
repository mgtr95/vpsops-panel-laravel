from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import AuthUser, assert_can, require_action, require_priv
from app.services import container_alerts_svc, docker_svc, mail_svc, recipients_svc

router = APIRouter(
    prefix="/api/docker",
    tags=["docker"],
    dependencies=[Depends(require_priv("docker"))],
)


@router.get("/containers")
def containers(_user: AuthUser, all: bool = True):
    return docker_svc.list_containers(all)


@router.get("/containers/{name}")
def inspect(name: str, user: AuthUser):
    assert_can(user, "docker", "inspect")
    try:
        return docker_svc.container_inspect(name)
    except Exception as e:
        raise HTTPException(404, str(e)) from e


class ActionBody(BaseModel):
    action: str


@router.post("/containers/{name}/action")
def action(name: str, body: ActionBody, user: AuthUser):
    allowed = {"start", "stop", "restart", "remove"}
    if body.action not in allowed:
        raise HTTPException(400, f"Unknown action: {body.action}")
    assert_can(user, "docker", body.action)
    try:
        return docker_svc.container_action(name, body.action)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/containers/{name}/logs")
def logs(
    name: str,
    user: AuthUser,
    tail: int = Query(200, ge=1, le=5000),
):
    assert_can(user, "docker", "logs")
    try:
        return {"logs": docker_svc.container_logs(name, tail)}
    except Exception as e:
        raise HTTPException(404, str(e)) from e


@router.get("/images")
def images(_user: AuthUser):
    return docker_svc.list_images()


@router.post("/prune/images", dependencies=[Depends(require_action("docker", "prune"))])
def prune_images(_user: AuthUser):
    return docker_svc.prune_images()


@router.post("/prune/builder", dependencies=[Depends(require_action("docker", "prune"))])
def prune_builder(_user: AuthUser):
    return docker_svc.prune_builder()


@router.get("/compose")
def compose_projects(_user: AuthUser):
    return docker_svc.discover_compose_projects()


@router.get("/df")
def df(_user: AuthUser):
    return {"disk": docker_svc.disk_usage(), "docker": docker_svc.system_df()}


class ContainerAlertsBody(BaseModel):
    enabled: bool = False
    notify: dict = {}
    events: dict = {}
    scan_logs: bool = False
    cooldown_minutes: int = 15


@router.get("/alerts")
def get_alerts(_user: AuthUser):
    config = container_alerts_svc.get_config()
    state = container_alerts_svc.load_state()
    return {
        "config": config,
        "mail_accounts": mail_svc.list_accounts(),
        "recipients": recipients_svc.list_recipients(),
        "last_check": state.get("last_check"),
    }


@router.put("/alerts")
def save_alerts(body: ContainerAlertsBody, _user: AuthUser):
    try:
        config = container_alerts_svc.save_config(body.model_dump())
        return {"ok": True, "config": config}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/alerts/test")
async def test_alerts(_user: AuthUser):
    try:
        await container_alerts_svc.send_test_alert()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
