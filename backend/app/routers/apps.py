from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import AuthUser, assert_can, require_action, require_priv
from app.services import deploy_svc, discover_svc, github_svc, pipeline_svc, site_svc

router = APIRouter(
    prefix="/api/apps",
    tags=["apps"],
    dependencies=[Depends(require_priv("apps"))],
)


def _with_status(app: dict, running: dict | None = None) -> dict:
    out = dict(app)
    if deploy_svc.uses_laravel_pipeline(app):
        latest = pipeline_svc.latest_pipeline(app["id"])
        out["latest_pipeline"] = latest
    elif deploy_svc.github_linked(app) or app.get("source") != "github":
        latest = deploy_svc.latest_job(app["id"])
        if latest:
            out["latest_job"] = {
                k: latest.get(k)
                for k in ("id", "status", "started_at", "finished_at", "sha", "trigger")
            }
    if running is None:
        running = discover_svc.running_map()
    out["running"] = running.get(app["id"])
    if not out.get("kind") and (out.get("detect") or {}).get("laravel"):
        out["kind"] = "laravel"
    out["managed"] = deploy_svc.uses_laravel_pipeline(out)
    out["needs_template"] = bool(out.get("needs_template") and not out.get("managed"))
    latest_pipe = out.get("latest_pipeline")
    out["can_adopt"] = pipeline_svc.show_adopt_form(out, latest_pipe)
    if out.get("managed"):
        from app.services import laravel_svc

        out["php"] = laravel_svc.normalize_php(out.get("php"))
    if out.get("kind") == "laravel" and out.get("path") and out["can_adopt"]:
        from pathlib import Path

        from app.services import adopt_data_svc, laravel_svc

        try:
            out["suggested_addons"] = laravel_svc.infer_addons(Path(app["path"]))
        except Exception:
            out["suggested_addons"] = None
        try:
            out["source_database"] = adopt_data_svc.summarize_source(app)
        except Exception:
            out["source_database"] = None
    return out


@router.get("")
def apps(_user: AuthUser):
    try:
        discover_svc.sync()
    except Exception:
        pass
    running = discover_svc.running_map()
    return [_with_status(a, running) for a in deploy_svc.list_apps()]


@router.get("/jobs")
def jobs(_user: AuthUser):
    return deploy_svc.list_jobs()


@router.get("/jobs/{job_id}")
def job(job_id: str, _user: AuthUser):
    data = deploy_svc.get_job(job_id)
    if not data:
        raise HTTPException(404, "Job not found")
    return data


@router.get("/jobs/{job_id}/stream")
async def stream(job_id: str, _user: AuthUser):
    return StreamingResponse(
        deploy_svc.stream_job_log(job_id),
        media_type="text/event-stream",
    )


@router.get("/pipelines")
def pipelines(_user: AuthUser, app_id: str | None = None):
    return pipeline_svc.list_pipelines(app_id)


@router.get("/pipelines/{pipeline_id}")
def pipeline(pipeline_id: str, _user: AuthUser):
    data = pipeline_svc.get_pipeline(pipeline_id)
    if not data:
        raise HTTPException(404, "Deployment not found")
    return pipeline_svc._public(data)


@router.get("/pipelines/{pipeline_id}/stream")
async def stream_pipeline(pipeline_id: str, _user: AuthUser):
    return StreamingResponse(
        pipeline_svc.stream_pipeline(pipeline_id),
        media_type="text/event-stream",
    )


class AddonsBody(BaseModel):
    database: str = "mysql"
    redis: bool = False
    queue: bool = False
    scheduler: bool = False
    mailpit: bool = False
    packages: str | list[str] = ""
    php_extensions: str | list[str] = ""


class CreateAppBody(BaseModel):
    owner: str
    repo: str
    branch: str = "main"
    name: str | None = None
    domain: str = ""
    www: bool = True
    auto_deploy: bool = True
    run_migrations: bool = True
    addons: AddonsBody = Field(default_factory=AddonsBody)


class FromFolderBody(BaseModel):
    path: str
    name: str | None = None
    domain: str = ""
    www: bool = True
    run_migrations: bool = True
    migrate_data: bool = True
    addons: AddonsBody = Field(default_factory=AddonsBody)


@router.post("/from-folder", dependencies=[Depends(require_action("apps", "create"))])
async def from_folder(body: FromFolderBody, _user: AuthUser):
    try:
        return await pipeline_svc.register_local_laravel(body.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("")
async def create_app(body: CreateAppBody, user: AuthUser):
    assert_can(user, "apps", "create")
    try:
        return await pipeline_svc.create_laravel_app(body.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/site/dns")
def dns_help(_user: AuthUser, domain: str = "", www: bool = True):
    nginx = site_svc.nginx_available()
    if domain.strip():
        check = site_svc.check_dns(domain, www)
        return {**check, "nginx": nginx}
    return {
        "public_ip": site_svc.public_ip(),
        "instructions": site_svc.dns_instructions(site_svc.public_ip()),
        "nginx": nginx,
        "ok": False,
        "records": [],
    }


@router.get("/{app_id}")
def app_detail(app_id: str, _user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    out = _with_status(app)
    out["pipelines"] = pipeline_svc.list_pipelines(app_id, limit=20)
    out["jobs"] = deploy_svc.list_jobs(limit=20, app_id=app_id)
    out["nginx"] = site_svc.nginx_available()
    if app.get("domain"):
        out["dns"] = site_svc.check_dns(app["domain"], bool(app.get("www", True)))
    return out


class DeployBody(BaseModel):
    pull: bool = True
    rebuild: bool = True
    restart: bool = False


@router.post("/{app_id}/deploy")
async def deploy(app_id: str, body: DeployBody, user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    restart_only = body.restart and not body.pull and not body.rebuild
    assert_can(user, "apps", "restart" if restart_only else "deploy")
    try:
        if deploy_svc.uses_laravel_pipeline(app):
            return await pipeline_svc.start_pipeline(app_id, trigger="manual")
        return await deploy_svc.run_deploy(
            app_id,
            do_pull=body.pull,
            do_rebuild=body.rebuild,
            do_restart=body.restart,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class PatchAppBody(BaseModel):
    auto_deploy: bool | None = None
    name: str | None = None
    branch: str | None = None


class LinkGithubBody(BaseModel):
    owner: str
    repo: str
    branch: str = "main"
    auto_deploy: bool = True


async def _github_link_payload(body: LinkGithubBody) -> dict:
    if not github_svc.load_install():
        raise HTTPException(400, "Connect GitHub first.")
    owner = (body.owner or "").strip()
    repo = (body.repo or "").strip()
    branch = (body.branch or "main").strip()
    if not owner or not repo:
        raise HTTPException(400, "Repository is required.")
    full_name = f"{owner}/{repo}"
    try:
        await github_svc.get_repo(owner, repo)
    except Exception as e:
        raise HTTPException(
            400,
            f"{full_name} is not in the GitHub App installation. Grant access first. ({e})",
        ) from e
    try:
        branches = await github_svc.list_branches(owner, repo)
    except Exception as e:
        raise HTTPException(400, f"Could not list branches: {e}") from e
    if branches and branch not in branches:
        raise HTTPException(400, f"Branch {branch} was not found on {full_name}.")
    return {
        "full_name": full_name,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "auto_deploy": bool(body.auto_deploy),
    }


@router.post("/{app_id}/github", dependencies=[Depends(require_action("apps", "github"))])
async def link_github(app_id: str, body: LinkGithubBody, _user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    if app.get("source") == "github":
        raise HTTPException(
            400,
            "This app was created from GitHub in the panel. Change the branch on the app page.",
        )
    app["github"] = await _github_link_payload(body)
    from pathlib import Path

    from app.services import laravel_svc

    path = Path(app.get("path") or "")
    detected = laravel_svc.detect_local(path) if path.is_dir() else {}
    if detected.get("laravel"):
        app["detect"] = {**(app.get("detect") or {}), **detected}
        # Do not apply the panel template here. Linking GitHub on a discovered
        # app must leave "Switch to panel template" available; auto-deploy
        # should keep using the current compose until that switch succeeds.
    deploy_svc.upsert_app(app)
    return _with_status(deploy_svc.get_app(app_id) or app)


@router.delete("/{app_id}/github", dependencies=[Depends(require_action("apps", "github"))])
def unlink_github(app_id: str, _user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    if app.get("source") == "github" and not app.get("discovered"):
        raise HTTPException(
            400,
            "This app was created from GitHub in the panel. Remove the app instead of unlinking.",
        )
    if "github" in app:
        app["github"] = None
        if app.get("managed") and app.get("source") == "github":
            app["source"] = "local"
        deploy_svc.upsert_app(app)
    return _with_status(deploy_svc.get_app(app_id) or app)


@router.patch("/{app_id}", dependencies=[Depends(require_action("apps", "deploy"))])
async def patch_app(app_id: str, body: PatchAppBody, _user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    if body.name:
        app["name"] = body.name.strip()
    gh = dict(app.get("github") or {})
    if body.auto_deploy is not None or body.branch is not None:
        if not gh.get("full_name"):
            raise HTTPException(400, "Link a GitHub repository first.")
    if body.auto_deploy is not None:
        gh["auto_deploy"] = body.auto_deploy
    if body.branch is not None:
        branch = body.branch.strip()
        if not branch:
            raise HTTPException(400, "Branch is required.")
        owner = gh.get("owner") or ""
        repo = gh.get("repo") or ""
        if owner and repo:
            try:
                branches = await github_svc.list_branches(owner, repo)
            except Exception as e:
                raise HTTPException(400, f"Could not list branches: {e}") from e
            if branches and branch not in branches:
                raise HTTPException(400, f"Branch {branch} was not found on {gh.get('full_name')}.")
        gh["branch"] = branch
    if gh:
        app["github"] = gh
    deploy_svc.upsert_app(app)
    return _with_status(deploy_svc.get_app(app_id) or app)


class PhpLimitsBody(BaseModel):
    upload_max_filesize_mb: int | None = None
    post_max_size_mb: int | None = None
    memory_limit_mb: int | None = None
    max_execution_time: int | None = None
    max_input_time: int | None = None
    client_max_body_size_mb: int | None = None


@router.put("/{app_id}/php", dependencies=[Depends(require_action("apps", "deploy"))])
async def put_app_php(app_id: str, body: PhpLimitsBody, _user: AuthUser):
    """Update per-app PHP upload/runtime limits and apply without an image rebuild."""
    from pathlib import Path

    from app.services import laravel_svc

    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    if not deploy_svc.uses_laravel_pipeline(app):
        raise HTTPException(400, "Only panel-managed Laravel apps support PHP limits.")
    path = Path(deploy_svc.compose_cwd(app))
    if not path.is_dir():
        raise HTTPException(400, f"App folder not found: {path}")

    current = laravel_svc.normalize_php(app.get("php"))
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    limits = laravel_svc.normalize_php({**current, **patch})
    app["php"] = limits
    deploy_svc.upsert_app(app)

    addons = laravel_svc.normalize_addons(app.get("addons") or {})
    try:
        laravel_svc.sync_php_runtime(path, app_id, addons, limits)
        await laravel_svc.compose_up_no_build(path, app_id)
    except Exception as e:
        raise HTTPException(400, str(e)) from e

    nginx = None
    try:
        nginx = await site_svc.refresh_vhost_body_size(app)
    except Exception as e:
        # PHP limits already applied; surface nginx issues without rolling back.
        nginx = {"ok": False, "error": str(e)}

    out = _with_status(deploy_svc.get_app(app_id) or app)
    out["nginx_reload"] = nginx
    return out


class DomainBody(BaseModel):
    domain: str
    www: bool = True


@router.post("/{app_id}/domain/check", dependencies=[Depends(require_action("apps", "domain"))])
def domain_check(app_id: str, body: DomainBody, _user: AuthUser):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    return site_svc.check_dns(body.domain, body.www)


@router.post("/{app_id}/domain", dependencies=[Depends(require_action("apps", "domain"))])
async def attach_domain(app_id: str, body: DomainBody, _user: AuthUser):
    try:
        return await site_svc.attach_domain(app_id, body.domain, body.www)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


class AdoptBody(BaseModel):
    addons: AddonsBody = Field(default_factory=AddonsBody)
    run_migrations: bool = True
    migrate_data: bool = True


@router.post("/{app_id}/adopt", dependencies=[Depends(require_action("apps", "create"))])
async def adopt_app(app_id: str, body: AdoptBody, _user: AuthUser):
    try:
        return await pipeline_svc.adopt_laravel_app(app_id, body.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{app_id}", dependencies=[Depends(require_action("apps", "delete"))])
async def delete_app(app_id: str, _user: AuthUser, delete_files: bool = False):
    app = deploy_svc.get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    if not deploy_svc.uses_laravel_pipeline(app):
        raise HTTPException(400, "Only panel-managed Laravel apps can be removed from here.")
    try:
        return await pipeline_svc.remove_laravel_app(app_id, delete_files=delete_files)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
