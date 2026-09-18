from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import AuthUser, require_action, require_priv
from app.services import github_svc

router = APIRouter(prefix="/api/github", tags=["github"])


def _redirect_apps(query: str = "") -> RedirectResponse:
    target = "/apps/new" + (f"?{query}" if query else "")
    return RedirectResponse(target, status_code=302)


@router.get("/status", dependencies=[Depends(require_priv("apps"))])
def status(_user: AuthUser):
    return github_svc.status_payload()


@router.get("/connect", dependencies=[Depends(require_action("apps", "github"))])
def connect(_user: AuthUser, request: Request):
    base = github_svc.public_base_url(request)
    app = github_svc.load_app()
    if not app:
        state = github_svc.new_state("manifest", {"public_url": base})
        manifest = github_svc.build_manifest(base)
        action = html.escape(f"https://github.com/settings/apps/new?state={state}", quote=True)
        value = html.escape(json.dumps(manifest), quote=True)
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Connect GitHub</title></head>
<body>
<p>Redirecting to GitHub to create the app…</p>
<form id="f" method="post" action="{action}">
  <input type="hidden" name="manifest" value="{value}">
  <button type="submit">Continue to GitHub</button>
</form>
<script>document.getElementById("f").submit()</script>
</body>
</html>"""
        )
    if not github_svc.load_install():
        state = github_svc.new_state("install", {"public_url": base})
        return RedirectResponse(github_svc.install_url(state), status_code=302)
    return _redirect_apps("github=connected")


@router.get("/manifest/callback")
async def manifest_callback(request: Request, code: str = "", state: str = ""):
    stored = github_svc.pop_state(state, "manifest") if state else None
    if not stored:
        raise HTTPException(400, "GitHub setup expired. Try Connect GitHub again.")
    if not code:
        raise HTTPException(400, "GitHub did not return a setup code.")
    base = stored.get("public_url") or github_svc.public_base_url(request)
    try:
        app = await github_svc.convert_manifest(code, base)
    except Exception as e:
        raise HTTPException(400, f"Could not finish GitHub App setup: {e}") from e
    install_state = github_svc.new_state("install", {"public_url": base})
    slug = app.get("slug")
    if not slug:
        return _redirect_apps("github=app")
    return RedirectResponse(
        f"https://github.com/apps/{slug}/installations/new?state={install_state}",
        status_code=302,
    )


@router.get("/setup")
async def setup(installation_id: int | None = None, setup_action: str = "", state: str = ""):
    if state:
        github_svc.pop_state(state)
    if not installation_id:
        return _redirect_apps("github=error")
    try:
        await github_svc.fetch_installation(installation_id)
    except Exception as e:
        raise HTTPException(400, f"Could not save GitHub access: {e}") from e
    action = setup_action or "install"
    return _redirect_apps(f"github={action}")


@router.get("/callback")
async def oauth_callback():
    return _redirect_apps("github=connected")


@router.get("/repos", dependencies=[Depends(require_priv("apps"))])
async def repos(_user: AuthUser):
    if not github_svc.load_install():
        raise HTTPException(400, "Connect GitHub first.")
    try:
        return await github_svc.list_repos()
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/repos/{owner}/{repo}/preview", dependencies=[Depends(require_priv("apps"))])
async def preview(owner: str, repo: str, _user: AuthUser, branch: str | None = None):
    from app.services import laravel_svc

    if not branch:
        meta = await github_svc.get_repo(owner, repo)
        branch = meta.get("default_branch") or "main"
    try:
        return await laravel_svc.preview_repo(owner, repo, branch)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.post("/disconnect", dependencies=[Depends(require_action("apps", "github"))])
def disconnect(_user: AuthUser):
    github_svc.clear_install()
    return {"ok": True}


@router.post("/reset", dependencies=[Depends(require_action("apps", "github"))])
def reset(_user: AuthUser):
    github_svc.clear_app()
    return {"ok": True}


@router.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    if not github_svc.verify_webhook(body, sig):
        raise HTTPException(401, "Invalid signature")
    event = request.headers.get("x-github-event", "")
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    if event == "ping":
        return {"ok": True, "pong": True}
    if event == "installation":
        action = payload.get("action")
        inst = payload.get("installation") or {}
        if action in ("deleted", "suspend"):
            current = github_svc.load_install()
            if current and str(current.get("installation_id")) == str(inst.get("id")):
                github_svc.clear_install()
        elif inst.get("id"):
            await github_svc.fetch_installation(int(inst["id"]))
        return {"ok": True}
    if event != "push":
        return {"ok": True, "ignored": event}

    from app.services import deploy_svc, pipeline_svc

    ref = payload.get("ref") or ""
    if payload.get("deleted"):
        return {"ok": True, "ignored": "deleted"}
    full_name = (payload.get("repository") or {}).get("full_name") or ""
    sha = payload.get("after") or ""
    head = payload.get("head_commit") or {}
    branch = ref.split("/")[-1] if ref.startswith("refs/heads/") else ""
    commit = {
        "sha": sha,
        "message": (head.get("message") or "").split("\n", 1)[0],
        "author": (head.get("author") or {}).get("name")
        or (payload.get("pusher") or {}).get("name"),
        "html_url": head.get("url"),
    }
    started = []
    for app in deploy_svc.list_apps():
        gh = app.get("github") or {}
        if not gh.get("full_name"):
            continue
        if gh.get("full_name") != full_name:
            continue
        if not gh.get("auto_deploy", True):
            continue
        if (gh.get("branch") or "main") != branch:
            continue
        try:
            if deploy_svc.uses_laravel_pipeline(app):
                pipe = await pipeline_svc.start_pipeline(
                    app["id"],
                    trigger="push",
                    sha=sha,
                    commit=commit,
                )
                started.append({"app_id": app["id"], "pipeline_id": pipe["id"]})
            else:
                job = await deploy_svc.run_deploy(
                    app["id"],
                    do_pull=True,
                    do_rebuild=True,
                    do_restart=False,
                    sha=sha,
                    trigger="push",
                )
                started.append({"app_id": app["id"], "job_id": job["id"]})
        except Exception as e:
            started.append({"app_id": app["id"], "error": str(e)})
    return {"ok": True, "started": started}
