"""Built-in Laravel CI/CD pipeline (checkout → backup → build → go live → data → migrate → health)."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from app.config import get_settings
from app.services import deploy_svc, docker_svc, github_svc, laravel_svc
from app.utils import read_json, write_json


def _redact(text: str) -> str:
    return github_svc.redact_secrets(text)

STEPS = (
    ("checkout", "Checkout"),
    ("backup", "Database backup"),
    ("php", "PHP packages"),
    ("frontend", "Frontend"),
    ("image", "Image"),
    ("golive", "Go live"),
    ("data", "Copy data"),
    ("migrate", "Database"),
    ("health", "Health"),
)

_locks: dict[str, asyncio.Lock] = {}
_pipelines: dict[str, dict] = {}


def _lock(app_id: str) -> asyncio.Lock:
    if app_id not in _locks:
        _locks[app_id] = asyncio.Lock()
    return _locks[app_id]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(pipeline_id: str) -> Path:
    settings = get_settings()
    settings.pipelines_path.mkdir(parents=True, exist_ok=True)
    return settings.pipelines_path / f"{pipeline_id}.json"


def _save(pipeline: dict) -> None:
    write_json(_path(pipeline["id"]), pipeline)
    _pipelines[pipeline["id"]] = pipeline


def get_pipeline(pipeline_id: str) -> dict | None:
    if pipeline_id in _pipelines:
        return _pipelines[pipeline_id]
    return read_json(_path(pipeline_id), None)


def list_pipelines(app_id: str | None = None, limit: int = 40) -> list[dict]:
    settings = get_settings()
    settings.pipelines_path.mkdir(parents=True, exist_ok=True)
    files = sorted(settings.pipelines_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        data = read_json(f, None)
        if not data:
            continue
        if app_id and data.get("app_id") != app_id:
            continue
        out.append(_summary(data))
        if len(out) >= limit:
            break
    return out


def _summary(pipeline: dict) -> dict:
    steps = []
    for step in pipeline.get("steps") or []:
        item = {k: v for k, v in step.items() if k != "log"}
        item["log_bytes"] = len(step.get("log") or "")
        steps.append(item)
    return {**{k: v for k, v in pipeline.items() if k != "steps"}, "steps": steps}


def _public(pipeline: dict) -> dict:
    steps = []
    for step in pipeline.get("steps") or []:
        log = step.get("log") or ""
        steps.append({**step, "log": log, "log_bytes": len(log)})
    return {**pipeline, "steps": steps}


def latest_pipeline(app_id: str) -> dict | None:
    items = list_pipelines(app_id, limit=1)
    return items[0] if items else None


def latest_adopt_pipeline(app_id: str) -> dict | None:
    if not app_id:
        return None
    for item in list_pipelines(app_id, limit=40):
        if item.get("trigger") == "adopt":
            return item
    return None


def show_adopt_form(app: dict, latest: dict | None = None) -> bool:
    """Switch UI stays until an adopt pipeline succeeds.

    Linking GitHub must not hide this. A later git-push pipeline is not a template switch.
    Apps created from GitHub in the wizard are already on the template.
    """
    if not app or app.get("kind") != "laravel":
        return False
    if app.get("source") == "github" and not app.get("discovered"):
        return False
    pipe = latest if latest and latest.get("trigger") == "adopt" else None
    if pipe is None:
        pipe = latest_adopt_pipeline(app.get("id") or "")
    status = (pipe or {}).get("status")
    if status in ("queued", "running", "pending", "success"):
        return False
    return True


def _new_pipeline(app: dict, trigger: str, commit: dict) -> dict:
    pid = str(uuid.uuid4())[:8]
    steps = []
    has_frontend = bool((app.get("detect") or {}).get("has_frontend"))
    run_migrate = bool(app.get("run_migrations", True)) and (app.get("addons") or {}).get("database") not in (
        None,
        "none",
        "",
    )
    is_adopt = trigger == "adopt"
    for key, title in STEPS:
        status = "pending"
        if key == "frontend" and not has_frontend:
            status = "skipped"
        if key == "migrate" and not run_migrate:
            status = "skipped"
        if key in ("backup", "data") and not is_adopt:
            status = "skipped"
        steps.append({"id": key, "title": title, "status": status, "log": "", "started_at": None, "finished_at": None})
    return {
        "id": pid,
        "app_id": app["id"],
        "status": "queued",
        "trigger": trigger,
        "commit": commit,
        "started_at": _now(),
        "finished_at": None,
        "error": None,
        "steps": steps,
    }


def _step(pipeline: dict, key: str) -> dict:
    for step in pipeline["steps"]:
        if step["id"] == key:
            return step
    raise KeyError(key)


def _append(pipeline: dict, key: str, text: str) -> None:
    step = _step(pipeline, key)
    step["log"] = (step.get("log") or "") + text
    if len(step["log"]) > 200_000:
        step["log"] = step["log"][-180_000:]
    _save(pipeline)


async def _run_cmd(cmd: list[str], cwd: str, pipeline: dict, key: str, env: dict | None = None) -> int:
    pretty = _redact(" ".join(cmd))
    _append(pipeline, key, f"$ {pretty}\n")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=merged,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        _append(pipeline, key, _redact(line.decode("utf-8", errors="replace")))
    return await proc.wait()


async def _git(token: str, args: list[str], cwd: str, pipeline: dict, key: str) -> int:
    cmd = ["git", *github_svc.git_config_args(token, cwd), *args]
    return await _run_cmd(cmd, cwd, pipeline, key, env={"GIT_TERMINAL_PROMPT": "0"})


async def start_pipeline(
    app_id: str,
    *,
    trigger: str = "manual",
    sha: str | None = None,
    commit: dict | None = None,
) -> dict:
    app = deploy_svc.get_app(app_id)
    if not app:
        raise ValueError(f"Unknown app: {app_id}")
    if not deploy_svc.uses_laravel_pipeline(app):
        raise ValueError("Pipelines are only for panel-managed Laravel apps")

    lock = _lock(app_id)
    if lock.locked():
        raise RuntimeError("A deployment is already running for this app")

    gh = app.get("github") or {}
    info = commit or {}
    if gh.get("full_name"):
        owner, repo = gh["full_name"].split("/", 1)
        branch = gh.get("branch") or "main"
        if not sha:
            latest = await github_svc.latest_commit(owner, repo, branch)
            sha = latest["sha"]
            info = latest
        else:
            info = {"sha": sha, **info}
    else:
        sha = sha or "local"
        info = {"sha": sha, "message": info.get("message") or "Files on this server", **info}

    pipeline = _new_pipeline(app, trigger, info)
    _save(pipeline)

    asyncio.create_task(_work(app_id, pipeline["id"], sha))
    return pipeline


async def _work(app_id: str, pipeline_id: str, sha: str) -> None:
    lock = _lock(app_id)
    async with lock:
        pipeline = get_pipeline(pipeline_id)
        app = deploy_svc.get_app(app_id)
        if not pipeline or not app:
            return
        pipeline["status"] = "running"
        _save(pipeline)
        gh = app.get("github") or {}
        owner = repo = ""
        if gh.get("full_name"):
            owner, repo = gh["full_name"].split("/", 1)
        target_url = None
        public = (github_svc.load_app() or {}).get("public_url")
        if public:
            target_url = f"{public}/apps/{app_id}"
        if owner and repo and sha and sha != "local":
            await github_svc.set_commit_status(owner, repo, sha, "pending", "Deploying on VPS Dashboard", target_url)
        try:
            await _run_pipeline(app, pipeline, sha)
            pipeline["status"] = "success"
            finished = deploy_svc.get_app(app_id) or app
            if finished.get("pending_adopt"):
                finished["pending_adopt"] = None
                deploy_svc.upsert_app(finished)
            if owner and repo and sha and sha != "local":
                await github_svc.set_commit_status(owner, repo, sha, "success", "Live on VPS Dashboard", target_url)
        except Exception as e:
            pipeline["status"] = "failed"
            pipeline["error"] = str(e)
            for step in pipeline["steps"]:
                if step["status"] == "running":
                    step["status"] = "failed"
                    step["finished_at"] = _now()
                    step["log"] = (step.get("log") or "") + f"\nERROR: {e}\n"
            if owner and repo and sha and sha != "local":
                await github_svc.set_commit_status(owner, repo, sha, "failure", str(e)[:140], target_url)
        finally:
            pipeline["finished_at"] = _now()
            _save(pipeline)


async def _run_pipeline(app: dict, pipeline: dict, sha: str) -> None:
    path = Path(app["path"])
    slug = app["id"]

    await _step_checkout(pipeline, app, path, sha)
    app = deploy_svc.get_app(app["id"]) or app
    detected = laravel_svc.detect_local(path)
    if not detected.get("laravel"):
        raise RuntimeError(detected.get("reason") or "Not a Laravel app")
    if _step(pipeline, "backup")["status"] != "skipped":
        await _step_backup(pipeline, app)
        app = deploy_svc.get_app(app["id"]) or app
    app["detect"] = {**(app.get("detect") or {}), **detected}
    addons = laravel_svc.normalize_addons(app.get("addons") or {})
    app["addons"] = addons
    app["php"] = laravel_svc.normalize_php(app.get("php"))
    laravel_svc.apply_panel_template(app, addons)
    deploy_svc.upsert_app(app)
    php = detected.get("php") or "8.3"
    from app.services import adopt_data_svc

    await adopt_data_svc.materialize_storage_on_host(
        app, lambda text: _append(pipeline, "checkout", text)
    )
    laravel_svc.write_runtime(
        path,
        slug,
        addons,
        app.get("domain") or "",
        php,
        app.get("php"),
    )
    docker_path = Path(deploy_svc.compose_cwd(app))

    has_frontend = bool(detected.get("has_frontend"))
    if has_frontend:
        _skip(
            pipeline,
            "php",
            "Composer and the frontend run in the production image build (one Docker context, storage stays on the host).",
        )
        _skip(
            pipeline,
            "frontend",
            "Vite runs inside the production image build.",
        )
    else:
        _skip(pipeline, "php", "Composer runs in the production image build.")
        _skip(pipeline, "frontend", "No package.json — skipping frontend build.")

    await _step_build(
        pipeline,
        "image",
        docker_path,
        slug,
        "prod",
        php=php,
        extra_tags=[f"panel-{slug}:latest", f"panel-{slug}:{(sha or 'local')[:12]}"],
    )
    await _prune_intermediate_images(pipeline, slug)
    await _step_up(pipeline, docker_path, slug)
    if _step(pipeline, "data")["status"] != "skipped":
        await _step_data(pipeline, app)
        app = deploy_svc.get_app(app["id"]) or app
    if pipeline.get("skip_migrate") and _step(pipeline, "migrate")["status"] != "skipped":
        _skip(pipeline, "migrate", pipeline.get("skip_migrate_reason") or "Schema already loaded from the database copy.")
    elif _step(pipeline, "migrate")["status"] != "skipped":
        await _step_migrate(pipeline, slug)
    try:
        await _step_health(pipeline, slug)
    except Exception:
        await _stop_runtime_after_failed_health(pipeline, docker_path, slug, addons)
        raise
    await _refresh_vhost(pipeline, app)
    if _step(pipeline, "data")["status"] != "skipped":
        await _step_stop_old(pipeline, app)


def _skip(pipeline: dict, key: str, reason: str) -> None:
    step = _step(pipeline, key)
    step["status"] = "skipped"
    step["log"] = reason + "\n"
    step["finished_at"] = _now()
    _save(pipeline)


def _begin(pipeline: dict, key: str) -> dict:
    step = _step(pipeline, key)
    if step["status"] == "skipped":
        return step
    step["status"] = "running"
    step["started_at"] = _now()
    _save(pipeline)
    return step


def _ok(pipeline: dict, key: str) -> None:
    step = _step(pipeline, key)
    step["status"] = "success"
    step["finished_at"] = _now()
    _save(pipeline)


async def _step_checkout(pipeline: dict, app: dict, path: Path, sha: str) -> None:
    _begin(pipeline, "checkout")
    gh = app.get("github") or {}
    if gh.get("full_name"):
        owner, repo = gh["full_name"].split("/", 1)
        branch = gh.get("branch") or "main"
        token = await github_svc.installation_token()
        origin = f"https://github.com/{owner}/{repo}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            if path.exists() and any(path.iterdir()):
                raise RuntimeError(f"App folder {path} already exists and is not a git repo")
            code = await _git(
                token,
                ["clone", "--branch", branch, "--single-branch", origin, str(path)],
                str(path.parent),
                pipeline,
                "checkout",
            )
            if code != 0:
                raise RuntimeError("git clone failed")
        code = await _git(token, ["fetch", "origin", sha], str(path), pipeline, "checkout")
        if code != 0:
            code = await _git(token, ["fetch", "origin"], str(path), pipeline, "checkout")
            if code != 0:
                raise RuntimeError("git fetch failed")
        code = await _git(token, ["checkout", "--force", sha], str(path), pipeline, "checkout")
        if code != 0:
            raise RuntimeError("git checkout failed")
        code = await _git(token, ["reset", "--hard", sha], str(path), pipeline, "checkout")
        if code != 0:
            raise RuntimeError("git reset failed")
        _ok(pipeline, "checkout")
        return

    if not path.exists():
        raise RuntimeError(f"App folder {path} does not exist")
    if (path / ".git").exists() and app.get("pull", True):
        cwd = str(path)
        code = await _run_cmd(
            ["git", *github_svc.git_safe_args(cwd), "pull", "--ff-only"],
            cwd,
            pipeline,
            "checkout",
        )
        if code != 0:
            _append(pipeline, "checkout", "git pull failed; using files already on disk.\n")
    else:
        _append(pipeline, "checkout", "No GitHub link — using Laravel files already on this server.\n")
    _ok(pipeline, "checkout")


async def _refresh_vhost(pipeline: dict, app: dict) -> None:
    domain = (app.get("domain") or "").strip()
    if not domain:
        return
    from app.services import certs_svc, site_svc

    try:
        https = site_svc.cert_exists(domain)
        site_svc.write_vhost(app, https=https)
        reload = await certs_svc.reload_nginx()
        _append(
            pipeline,
            "golive",
            f"nginx vhost for {domain} → {app['id']}_app:80"
            + (" (HTTPS)" if https else " (HTTP until Go live)")
            + f" reload={'ok' if reload.get('ok') else reload}\n",
        )
    except Exception as e:
        _append(pipeline, "golive", f"nginx vhost not updated: {e}\n")


def docker_prod_build_cmd(
    slug: str, php: str, extra_tags: list[str] | None = None, target: str = "prod"
) -> list[str]:
    tag = extra_tags[0] if extra_tags else f"panel-{slug}:{target}"
    cmd = [
        "docker",
        "build",
        "-f",
        "Dockerfile.panel",
        "--target",
        target,
        "--build-arg",
        f"PHP_VERSION={php}",
        "-t",
        tag,
    ]
    for extra in extra_tags[1:] if extra_tags else []:
        cmd.extend(["-t", extra])
    cmd.append(".")
    return cmd


async def _step_build(
    pipeline: dict,
    key: str,
    path: Path,
    slug: str,
    target: str,
    extra_tags: list[str] | None = None,
    php: str = "8.3",
) -> None:
    _begin(pipeline, key)
    _append(
        pipeline,
        key,
        f"PHP {php}; storage/ and uploads stay on the host bind mount, not in the image.\n",
    )
    cmd = docker_prod_build_cmd(slug, php, extra_tags=extra_tags, target=target)
    code = await _run_cmd(cmd, str(path), pipeline, key)
    if code != 0:
        raise RuntimeError(f"{key} build failed")
    _ok(pipeline, key)


async def _prune_intermediate_images(pipeline: dict, slug: str) -> None:
    """Drop vendor/assets tags left by older pipelines. They duplicated the whole app."""
    for tag in (f"panel-{slug}:vendor", f"panel-{slug}:assets"):
        await _run_cmd(["docker", "image", "rm", "-f", tag], "/", pipeline, "image")
    await _run_cmd(
        ["docker", "builder", "prune", "-f", "--keep-storage", "2GB"],
        "/",
        pipeline,
        "image",
    )


async def _stop_runtime_after_failed_health(
    pipeline: dict, path: Path, slug: str, addons: dict | None
) -> None:
    services = laravel_svc.runtime_services_to_stop(addons)
    _append(
        pipeline,
        "health",
        "Health failed — stopping "
        + ", ".join(services)
        + " so they do not restart-loop. Database is left running.\n",
    )
    cmd = laravel_svc.panel_compose_cmd(slug, "stop", *services)
    try:
        await _run_cmd(cmd, str(path), pipeline, "health")
    except Exception as e:
        _append(pipeline, "health", f"Could not stop runtime services: {e}\n")


async def _step_up(pipeline: dict, path: Path, slug: str) -> None:
    _begin(pipeline, "golive")
    cmd = laravel_svc.panel_compose_cmd(slug, "up", "-d", "--no-build")
    env_file = deploy_svc.detect_compose_env_file(path, "docker-compose.panel.yml")
    if env_file:
        cmd = [
            "docker",
            "compose",
            "--env-file",
            env_file,
            "-p",
            laravel_svc.panel_project_name(slug),
            "-f",
            "docker-compose.panel.yml",
            "up",
            "-d",
            "--no-build",
        ]
    code = await _run_cmd(cmd, str(path), pipeline, "golive")
    if code != 0:
        raise RuntimeError("compose up failed")
    _ok(pipeline, "golive")


async def _step_backup(pipeline: dict, app: dict) -> None:
    from app.services import adopt_data_svc

    _begin(pipeline, "backup")
    result = await adopt_data_svc.backup_existing_database(
        app, lambda text: _append(pipeline, "backup", text)
    )
    pending = dict(app.get("pending_adopt") or {})
    pending["backup_dir"] = result.get("dir")
    pending["dump"] = result.get("dump")
    app["pending_adopt"] = pending
    app["last_db_backup"] = {"path": result.get("dir"), "at": _now()}
    deploy_svc.upsert_app(app)
    _ok(pipeline, "backup")


async def _step_data(pipeline: dict, app: dict) -> None:
    from app.services import adopt_data_svc

    _begin(pipeline, "data")
    pending = dict(app.get("pending_adopt") or {})
    await adopt_data_svc.copy_storage_into_volumes(
        app, lambda text: _append(pipeline, "data", text)
    )
    migrate_data = pending.get("migrate_data", True)
    dump = pending.get("dump")
    backup_dir = pending.get("backup_dir")
    source = None
    if backup_dir:
        source_file = Path(backup_dir) / "source.json"
        if source_file.is_file():
            source = read_json(source_file, None)
    if not migrate_data:
        _append(
            pipeline,
            "data",
            "Existing database data was not copied (option left off). Storage was still synced onto the host folder.\n",
        )
    else:
        result = await adopt_data_svc.restore_or_convert(
            app,
            {"dir": backup_dir, "dump": dump, "source": source},
            lambda text: _append(pipeline, "data", text),
        )
        if result.get("restored_dump"):
            pipeline["skip_migrate"] = True
            pipeline["skip_migrate_reason"] = "Schema came from the restored database dump."
        elif result.get("converted"):
            pipeline["skip_migrate"] = True
            pipeline["skip_migrate_reason"] = "Schema was created while converting data."
    _save(pipeline)
    _ok(pipeline, "data")


async def _step_stop_old(pipeline: dict, app: dict) -> None:
    from app.services import adopt_data_svc

    addons = app.get("addons") or {}
    keep_db = (addons.get("database") or "none").lower() in {"none", ""}
    await adopt_data_svc.stop_old_compose(
        app,
        keep_database=keep_db,
        log=lambda text: _append(pipeline, "data", text),
    )


async def _step_migrate(pipeline: dict, slug: str) -> None:
    _begin(pipeline, "migrate")
    client = docker_svc.get_client()
    try:
        container = client.containers.get(f"{slug}_app")
    except Exception as e:
        raise RuntimeError(f"App container not found: {e}") from e
    _append(pipeline, "migrate", "$ php artisan migrate --force\n")
    result = await asyncio.to_thread(
        container.exec_run, ["php", "artisan", "migrate", "--force", "--no-interaction"]
    )
    out = (result.output or b"").decode("utf-8", errors="replace") if result.output else ""
    _append(pipeline, "migrate", out)
    if result.exit_code != 0:
        raise RuntimeError("migrate failed")
    _ok(pipeline, "migrate")


async def _step_health(pipeline: dict, slug: str) -> None:
    _begin(pipeline, "health")
    client = docker_svc.get_client()
    last = ""
    diagnosed = False
    for attempt in range(20):
        try:
            container = client.containers.get(f"{slug}_app")
            container.reload()
            if container.status != "running":
                last = f"container status: {container.status}"
                _append(pipeline, "health", last + "\n")
                await asyncio.sleep(2)
                continue
            result = await asyncio.to_thread(
                container.exec_run,
                [
                    "sh",
                    "-c",
                    # /up is Laravel 11+. Laravel 10 404s it; / often 302s to login.
                    # Probe one URL at a time so the log is "HTTP 302", not "404302".
                    "code=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/up); "
                    "if [ \"$code\" = \"404\" ] || [ \"$code\" = \"000\" ]; then "
                    "code=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/); "
                    "fi; "
                    "echo \"$code\"; "
                    "echo \"$code\" | grep -qE '^(2|3)'",
                ],
            )
            out = (result.output or b"").decode("utf-8", errors="replace") if result.output else ""
            last = f"HTTP {out.strip()} (exit {result.exit_code})"
            _append(pipeline, "health", last + "\n")
            if result.exit_code == 0 and out.strip() and out.strip() != "000":
                _ok(pipeline, "health")
                return
            if not diagnosed:
                diagnosed = True
                diag = await asyncio.to_thread(
                    container.exec_run,
                    [
                        "sh",
                        "-c",
                        "php -r 'echo \"PHP \".PHP_VERSION.PHP_EOL;' ; "
                        "php artisan --version 2>&1 | head -25",
                    ],
                )
                diag_out = (
                    (diag.output or b"").decode("utf-8", errors="replace") if diag.output else ""
                ).strip()
                if diag_out:
                    _append(pipeline, "health", diag_out + "\n")
        except Exception as e:
            last = str(e)
            _append(pipeline, "health", last + "\n")
        await asyncio.sleep(2)
    raise RuntimeError(f"App did not become healthy: {last}")


async def stream_pipeline(pipeline_id: str) -> AsyncIterator[str]:
    last = {}
    while True:
        pipeline = get_pipeline(pipeline_id)
        if not pipeline:
            yield 'data: {"error":"not found"}\n\n'
            return
        for step in pipeline.get("steps") or []:
            log = step.get("log") or ""
            prev = last.get(step["id"], "")
            if log != prev:
                last[step["id"]] = log
                chunk = log[len(prev) :]
                for line in chunk.splitlines(True):
                    yield f"event: log\ndata: {step['id']}|{line.rstrip()}\n\n"
            status_key = f"{step['id']}:status"
            if last.get(status_key) != step.get("status"):
                last[status_key] = step.get("status")
                yield f"event: step\ndata: {step['id']}|{step['status']}\n\n"
        if pipeline.get("status") in ("success", "failed"):
            yield f"event: done\ndata: {pipeline['status']}\n\n"
            return
        await asyncio.sleep(0.5)


async def create_laravel_app(body: dict) -> dict:
    owner = body["owner"].strip()
    repo = body["repo"].strip()
    branch = (body.get("branch") or "main").strip()
    preview = await laravel_svc.preview_repo(owner, repo, branch)
    if not preview.get("laravel"):
        raise ValueError(preview.get("reason") or "Not a Laravel app")
    addons = laravel_svc.normalize_addons(body.get("addons") or {})
    existing = {a["id"] for a in deploy_svc.list_apps()}
    name = (body.get("name") or repo).strip()
    slug = laravel_svc.unique_slug(name, existing)
    path = laravel_svc.app_dir(slug)
    domain = (body.get("domain") or "").strip().lower()
    app = laravel_svc.apply_panel_template(
        {
            "id": slug,
            "name": name,
            "path": str(path),
            "pull": True,
            "github": {
                "full_name": f"{owner}/{repo}",
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "auto_deploy": bool(body.get("auto_deploy", True)),
            },
            "domain": domain,
            "www": bool(body.get("www", True)),
            "run_migrations": bool(body.get("run_migrations", True)),
            "detect": preview,
        },
        addons,
    )
    deploy_svc.upsert_app(app)
    laravel_svc.upsert_database_config(app)
    pipeline = await start_pipeline(slug, trigger="create")
    return {"app": deploy_svc.get_app(slug), "pipeline": pipeline}


async def adopt_laravel_app(app_id: str, body: dict | None = None) -> dict:
    """Switch a folder already on this VPS onto the panel Docker template."""
    app = deploy_svc.get_app(app_id)
    if not app:
        raise ValueError(f"Unknown app: {app_id}")
    path = Path(app["path"])
    if not path.is_dir():
        raise ValueError(f"App folder not found: {path}")
    detected = laravel_svc.detect_local(path)
    if not detected.get("laravel"):
        raise ValueError(detected.get("reason") or "Not a Laravel app")
    body = body or {}
    addons = laravel_svc.normalize_addons(body.get("addons") or app.get("addons") or laravel_svc.infer_addons(path))
    if "run_migrations" in body:
        app["run_migrations"] = bool(body["run_migrations"])
    app["pending_adopt"] = {"migrate_data": bool(body.get("migrate_data", True))}
    app["detect"] = {**(app.get("detect") or {}), **detected}
    laravel_svc.apply_panel_template(app, addons)
    deploy_svc.upsert_app(app)
    laravel_svc.upsert_database_config(app)
    pipeline = await start_pipeline(app_id, trigger="adopt")
    return {"app": deploy_svc.get_app(app_id), "pipeline": pipeline}


async def register_local_laravel(body: dict) -> dict:
    """Register a Laravel folder under the apps root and deploy with the panel template."""
    settings = get_settings()
    raw = (body.get("path") or body.get("name") or "").strip()
    if not raw:
        raise ValueError("Folder name is required.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(settings.apps_root) / raw
    try:
        path = candidate.resolve()
        root = Path(settings.apps_root).resolve()
        path.relative_to(root)
    except (OSError, ValueError) as e:
        raise ValueError("Folder must be inside the apps directory.") from e
    if not path.is_dir():
        raise ValueError(f"Folder not found: {path}")
    detected = laravel_svc.detect_local(path)
    if not detected.get("laravel"):
        raise ValueError(detected.get("reason") or "Not a Laravel app")
    existing = {a["id"] for a in deploy_svc.list_apps()}
    by_path = {str(Path(a["path"])): a for a in deploy_svc.list_apps() if a.get("path")}
    if str(path) in by_path:
        return await adopt_laravel_app(by_path[str(path)]["id"], body)
    name = (body.get("name") or path.name).strip()
    slug = laravel_svc.unique_slug(name, existing)
    addons = laravel_svc.normalize_addons(body.get("addons") or laravel_svc.infer_addons(path))
    app = laravel_svc.apply_panel_template(
        {
            "id": slug,
            "name": name,
            "path": str(path),
            "pull": (path / ".git").is_dir(),
            "domain": (body.get("domain") or "").strip().lower(),
            "www": bool(body.get("www", True)),
            "run_migrations": bool(body.get("run_migrations", True)),
            "pending_adopt": {"migrate_data": bool(body.get("migrate_data", True))},
            "detect": detected,
        },
        addons,
    )
    deploy_svc.upsert_app(app)
    laravel_svc.upsert_database_config(app)
    pipeline = await start_pipeline(slug, trigger="adopt")
    return {"app": deploy_svc.get_app(slug), "pipeline": pipeline}


async def remove_laravel_app(app_id: str, delete_files: bool = False) -> dict:
    app = deploy_svc.get_app(app_id)
    if not app:
        raise ValueError(f"Unknown app: {app_id}")
    path = Path(app["path"])
    compose = path / "docker-compose.panel.yml"
    if compose.exists():
        proc = await asyncio.create_subprocess_exec(
            *laravel_svc.panel_compose_cmd(app_id, "down"),
            cwd=str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
    laravel_svc.remove_database_config(app_id)
    from app.services import proxy_svc

    conf = proxy_svc.conf_dir() / f"{app_id}.conf"
    if conf.exists():
        conf.unlink()
        try:
            from app.services import certs_svc

            await certs_svc.reload_nginx()
        except Exception:
            pass
    deploy_svc.delete_app(app_id)
    if delete_files and path.exists() and str(path).startswith(str(Path(get_settings().apps_root))):
        shutil.rmtree(path, ignore_errors=True)
    return {"ok": True, "id": app_id}
