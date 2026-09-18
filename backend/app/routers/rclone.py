from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import AuthUser, require_action, require_priv
from app.services import rclone_svc

router = APIRouter(
    prefix="/api/rclone",
    tags=["rclone"],
    dependencies=[Depends(require_priv("rclone"))],
)


@router.get("/remotes")
async def remotes(_user: AuthUser):
    try:
        return {"remotes": await rclone_svc.list_remotes()}
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/providers")
async def providers(_user: AuthUser):
    try:
        return await rclone_svc.get_providers()
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/config", dependencies=[Depends(require_action("rclone", "download_config"))])
async def config_dump(_user: AuthUser):
    try:
        return await rclone_svc.dump_config()
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/config/file", dependencies=[Depends(require_action("rclone", "download_config"))])
async def config_file(_user: AuthUser):
    path = "/config/rclone/rclone.conf"
    return FileResponse(path, filename="rclone.conf", media_type="text/plain")


class WizardStepBody(BaseModel):
    name: str
    type: str
    state: str | None = None
    result: str | None = None
    all: bool = False
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""


@router.post("/wizard/step", dependencies=[Depends(require_action("rclone", "add_remote"))])
async def wizard_step(body: WizardStepBody, _user: AuthUser):
    hints = {
        k: v
        for k, v in {
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "scope": body.scope,
        }.items()
        if v
    }
    try:
        return await rclone_svc.wizard_step(
            body.name,
            body.type,
            state=body.state,
            result=body.result,
            ask_all=body.all,
            oauth_hints=hints,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class WizardOAuthBody(BaseModel):
    type: str
    redirect_url: str
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""


@router.post("/wizard/oauth", dependencies=[Depends(require_action("rclone", "add_remote"))])
async def wizard_oauth(body: WizardOAuthBody, _user: AuthUser):
    from app.services import rclone_oauth

    try:
        token = await rclone_oauth.exchange_code(
            body.type,
            body.redirect_url,
            client_id=body.client_id or None,
            client_secret=body.client_secret or None,
            scope=body.scope or None,
        )
        return {"token": token}
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class WizardCancelBody(BaseModel):
    name: str = ""


@router.post("/wizard/cancel", dependencies=[Depends(require_action("rclone", "add_remote"))])
async def wizard_cancel(body: WizardCancelBody, _user: AuthUser):
    try:
        return await rclone_svc.wizard_cancel(body.name)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class RemoteCreate(BaseModel):
    name: str
    type: str
    parameters: dict[str, Any] = Field(default_factory=dict)


@router.post("/remotes", dependencies=[Depends(require_action("rclone", "add_remote"))])
async def create_remote(body: RemoteCreate, _user: AuthUser):
    try:
        return await rclone_svc.create_remote(body.name, body.type, body.parameters)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class RemoteUpdate(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


@router.put("/remotes/{name}", dependencies=[Depends(require_action("rclone", "add_remote"))])
async def update_remote(
    name: str, body: RemoteUpdate, _user: AuthUser
):
    try:
        return await rclone_svc.update_remote(name, body.parameters)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/remotes/{name}", dependencies=[Depends(require_action("rclone", "delete_remote"))])
async def delete_remote(name: str, _user: AuthUser):
    try:
        return await rclone_svc.delete_remote(name)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/remotes/{name}")
async def get_remote(name: str, _user: AuthUser):
    try:
        return await rclone_svc.get_remote(name)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/list")
async def list_path(
    _user: AuthUser,
    fs: str,
    remote: str = "",
    dirs_only: bool = False,
):
    try:
        return await rclone_svc.operations_list(fs, remote, dirs_only=dirs_only)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/about")
async def about(_user: AuthUser, fs: str):
    try:
        return await rclone_svc.about(fs)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class PathBody(BaseModel):
    fs: str
    remote: str = ""


@router.post("/mkdir", dependencies=[Depends(require_action("rclone", "mkdir"))])
async def mkdir(body: PathBody, _user: AuthUser):
    try:
        return await rclone_svc.mkdir(body.fs, body.remote)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("/deletefile", dependencies=[Depends(require_action("rclone", "delete"))])
async def deletefile(body: PathBody, _user: AuthUser):
    try:
        return await rclone_svc.delete_file(body.fs, body.remote)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("/purge", dependencies=[Depends(require_action("rclone", "delete"))])
async def purge(body: PathBody, _user: AuthUser):
    try:
        return await rclone_svc.purge(body.fs, body.remote)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class TransferBody(BaseModel):
    op: str  # sync | copy | move | check
    srcFs: str
    dstFs: str
    async_job: bool = True
    createEmptySrcDirs: bool = False


@router.post("/transfer", dependencies=[Depends(require_action("rclone", "transfer"))])
async def transfer(body: TransferBody, _user: AuthUser):
    try:
        return await rclone_svc.sync_copy_move(
            body.op,
            body.srcFs,
            body.dstFs,
            async_job=body.async_job,
            create_empty_src_dirs=body.createEmptySrcDirs,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/jobs")
async def jobs(_user: AuthUser):
    try:
        return await rclone_svc.job_list()
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@router.get("/jobs/{jobid}")
async def job_status(jobid: int, _user: AuthUser):
    try:
        return await rclone_svc.job_status(jobid)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/stats")
async def stats(_user: AuthUser):
    try:
        return await rclone_svc.core_stats()
    except Exception as e:
        raise HTTPException(502, str(e)) from e


class BwBody(BaseModel):
    rate: str


@router.post("/bwlimit", dependencies=[Depends(require_action("rclone", "raw_rc"))])
async def bwlimit(body: BwBody, _user: AuthUser):
    try:
        return await rclone_svc.core_bwlimit(body.rate)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class RawBody(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/rc", dependencies=[Depends(require_action("rclone", "raw_rc"))])
async def raw_rc(body: RawBody, _user: AuthUser):
    try:
        return await rclone_svc.raw(body.method, body.params)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
