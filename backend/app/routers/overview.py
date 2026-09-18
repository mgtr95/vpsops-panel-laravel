from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import AuthUser, require_action, require_priv
from app.services import backup_svc, certs_svc, docker_svc, mail_svc, notify_groups_svc, proxy_svc, rclone_svc, recipients_svc, system_svc

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
def overview(user: AuthUser):
    can = {
        "docker": user.has("docker"),
        "sites": user.has("sites"),
        "backups": user.has("backups"),
    }
    data = {
        "time": datetime.now(timezone.utc).isoformat(),
        "containers_total": 0,
        "containers_running": 0,
        "unhealthy": [],
        "disk": {},
        "system": {},
        "docker_df": {},
        "certs": [],
        "certs_expiring": [],
        "backups": [],
        "backup_jobs": [],
        "proxy": None,
        "can": can,
    }
    try:
        data["system"] = system_svc.snapshot()
    except Exception:
        data["system"] = {}
    if user.has("docker"):
        data.update(docker_svc.overview())
    else:
        data["disk"] = docker_svc.disk_usage()
    if user.has("sites"):
        certs = certs_svc.list_certificates()
        data["certs"] = certs
        data["certs_expiring"] = [c for c in certs if c.get("days_left", 999) <= 30]
        data["proxy"] = proxy_svc.status()
    if user.has("backups"):
        data["backups"] = backup_svc.list_history(10)
        data["backup_jobs"] = backup_svc.list_backup_jobs()
    data["can"] = can
    return data


backup_router = APIRouter(
    prefix="/api/backups",
    tags=["backups"],
    dependencies=[Depends(require_priv("backups"))],
)


class BackupJobBody(BaseModel):
    id: str | None = None
    name: str
    enabled: bool = True
    cron: str | None = None
    schedule: dict[str, Any] | None = None
    kind: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    destination: str = ""
    destination_remote: str = ""
    destination_folder: str = ""
    notify: dict[str, Any] | None = None


@backup_router.get("")
def list_jobs(_user: AuthUser):
    jobs = backup_svc.list_backup_jobs()
    for job in jobs:
        job["when"] = backup_svc.describe_schedule(job)
        if not job.get("schedule"):
            job["schedule"] = backup_svc.parse_cron(job.get("cron"))
        if not (job.get("schedule") or {}).get("timezone"):
            job.setdefault("schedule", {})["timezone"] = backup_svc.get_panel_timezone()
    return jobs


@backup_router.get("/targets")
async def targets(_user: AuthUser):
    data = backup_svc.list_backup_targets()
    try:
        data["remotes"] = await rclone_svc.list_remotes()
    except Exception:
        data["remotes"] = []
    data["mail_accounts"] = mail_svc.list_accounts()
    data["recipients"] = recipients_svc.list_recipients()
    data["notify_groups"] = notify_groups_svc.list_groups()
    return data


@backup_router.get("/history")
def history(_user: AuthUser):
    return backup_svc.list_history()


@backup_router.get("/apps/{app_id}/folders")
def list_app_folders(app_id: str, _user: AuthUser, path: str = ""):
    try:
        return backup_svc.list_app_source_dirs(app_id, path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@backup_router.post("", dependencies=[Depends(require_action("backups", "save"))])
def upsert(body: BackupJobBody, _user: AuthUser):
    try:
        return backup_svc.upsert_backup_job(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@backup_router.delete("/{job_id}", dependencies=[Depends(require_action("backups", "delete"))])
def delete(job_id: str, _user: AuthUser):
    if not backup_svc.delete_backup_job(job_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}


@backup_router.post("/{job_id}/run", dependencies=[Depends(require_action("backups", "run"))])
async def run(job_id: str, _user: AuthUser):
    try:
        return await backup_svc.run_backup_job(job_id, manual=True)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class NotifyGroupBody(BaseModel):
    id: str | None = None
    name: str
    job_ids: list[str] = Field(default_factory=list)
    notify: dict[str, Any] | None = None
    timeout_minutes: int = 120


@backup_router.get("/notify-groups")
def list_notify_groups(_user: AuthUser):
    return notify_groups_svc.list_groups()


@backup_router.post("/notify-groups", dependencies=[Depends(require_action("backups", "save"))])
def upsert_notify_group(body: NotifyGroupBody, _user: AuthUser):
    try:
        return notify_groups_svc.upsert_group(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@backup_router.delete(
    "/notify-groups/{group_id}",
    dependencies=[Depends(require_action("backups", "delete"))],
)
def delete_notify_group(group_id: str, _user: AuthUser):
    if not notify_groups_svc.delete_group(group_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}
