from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from app.config import get_settings
from app.services import github_svc
from app.utils import read_json, write_json

_locks: dict[str, asyncio.Lock] = {}
_jobs: dict[str, dict] = {}


def _lock(app_id: str) -> asyncio.Lock:
    if app_id not in _locks:
        _locks[app_id] = asyncio.Lock()
    return _locks[app_id]


def list_apps() -> list[dict]:
    settings = get_settings()
    # Prefer panel-config/apps.json; allow override in data/config.json["apps"]
    apps = read_json(settings.apps_config_path, None)
    if apps is None:
        apps = read_json(settings.config_path, {}).get("apps")
    return apps or []


def save_apps(apps: list[dict]) -> None:
    settings = get_settings()
    write_json(settings.apps_config_path, apps)


def upsert_app(app: dict) -> dict:
    apps = list_apps()
    found = False
    for i, existing in enumerate(apps):
        if existing.get("id") == app["id"]:
            merged = {**existing, **app}
            if "github" in app and not app.get("github"):
                merged.pop("github", None)
            if "pending_adopt" in app and not app.get("pending_adopt"):
                merged.pop("pending_adopt", None)
            apps[i] = merged
            found = True
            break
    if not found:
        apps.append(app)
    save_apps(apps)
    return get_app(app["id"]) or app


def delete_app(app_id: str) -> dict | None:
    apps = list_apps()
    remaining = [a for a in apps if a.get("id") != app_id]
    if len(remaining) == len(apps):
        return None
    removed = next(a for a in apps if a.get("id") == app_id)
    save_apps(remaining)
    return removed


def get_app(app_id: str) -> dict | None:
    for app in list_apps():
        if app["id"] == app_id:
            return app
    return None


def github_linked(app: dict | None) -> bool:
    return bool(app and (app.get("github") or {}).get("full_name"))


def uses_laravel_pipeline(app: dict | None) -> bool:
    if not app or app.get("kind") != "laravel":
        return False
    if app.get("managed"):
        return True
    if app.get("compose_file") == "docker-compose.panel.yml":
        return True
    return app.get("source") == "github"


def compose_cwd(app: dict) -> str:
    """Directory Docker Compose/build should use so bind mounts hit the host path."""
    panel = app["path"]
    host = get_settings().host_path(panel)
    if host != panel and Path(host).is_dir():
        return host
    return panel


_PROD_ENV_NAMES = (".env.prod", ".env.production")


def detect_compose_env_file(
    directory: Path | str,
    compose_file: str = "",
    explicit: str | None = None,
) -> str | None:
    """Env file for compose interpolation (${VAR} in yaml).

    Compose auto-loads `.env` from the project dir. Prod stacks that keep secrets
    in `.env.prod` (as some prod stacks do) need an explicit `--env-file`.
    """
    root = Path(directory)
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    name = Path(compose_file or "").name.lower()
    if "prod" in name:
        candidates.extend(_PROD_ENV_NAMES)
    seen: set[str] = set()
    for cand in candidates:
        key = cand.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        path = Path(key)
        if not path.is_absolute():
            path = root / key
        if path.is_file():
            try:
                return str(path.relative_to(root))
            except ValueError:
                return str(path)
    return None


def with_compose_env_file(cmd: list[str], app: dict, docker_cwd: str) -> list[str]:
    if len(cmd) < 2 or cmd[0] != "docker" or cmd[1] != "compose":
        return cmd
    if "--env-file" in cmd:
        return cmd
    env_file = detect_compose_env_file(
        docker_cwd,
        app.get("compose_file") or "",
        app.get("compose_env_file"),
    )
    if not env_file:
        return cmd
    return [cmd[0], cmd[1], "--env-file", env_file, *cmd[2:]]


def list_jobs(limit: int = 50, app_id: str | None = None) -> list[dict]:
    settings = get_settings()
    jobs_dir = settings.jobs_path
    jobs_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    result = []
    for f in files:
        data = read_json(f, None)
        if not data:
            continue
        if app_id and data.get("app_id") != app_id:
            continue
        result.append(data)
        if len(result) >= limit:
            break
    for jid, job in _jobs.items():
        if app_id and job.get("app_id") != app_id:
            continue
        if not any(r.get("id") == jid for r in result):
            result.insert(0, job)
    return result[:limit]


def latest_job(app_id: str) -> dict | None:
    items = list_jobs(limit=1, app_id=app_id)
    return items[0] if items else None


def get_job(job_id: str) -> dict | None:
    if job_id in _jobs:
        return _jobs[job_id]
    settings = get_settings()
    path = settings.jobs_path / f"{job_id}.json"
    return read_json(path, None)


def _save_job(job: dict) -> None:
    settings = get_settings()
    settings.jobs_path.mkdir(parents=True, exist_ok=True)
    write_json(settings.jobs_path / f"{job['id']}.json", job)
    _jobs[job["id"]] = job


def _log(job: dict, text: str) -> None:
    job["log"] = job.get("log", "") + github_svc.redact_secrets(text)
    _save_job(job)


async def _run_cmd(cmd: list[str], cwd: str, job: dict, env: dict | None = None) -> int:
    pretty = github_svc.redact_secrets(" ".join(cmd))
    _log(job, f"\n$ {pretty}\n")
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
        _log(job, line.decode("utf-8", errors="replace"))
    return await proc.wait()


async def _run_capture(cmd: list[str], cwd: str, env: dict | None = None) -> tuple[int, str]:
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
    out = await proc.stdout.read()
    code = await proc.wait()
    return code, out.decode("utf-8", errors="replace").strip()


async def _run_git(token: str, args: list[str], cwd: str, job: dict) -> int:
    cmd = ["git", *github_svc.git_config_args(token, cwd), *args]
    return await _run_cmd(cmd, cwd, job, env={"GIT_TERMINAL_PROMPT": "0"})


def _status_target(app_id: str) -> str | None:
    public = (github_svc.load_app() or {}).get("public_url")
    if public:
        return f"{public.rstrip('/')}/apps/{app_id}"
    return None


async def _set_commit_status(app: dict, sha: str | None, state: str, description: str) -> None:
    gh = app.get("github") or {}
    full_name = gh.get("full_name") or ""
    if not full_name or not sha:
        return
    owner, repo = full_name.split("/", 1)
    await github_svc.set_commit_status(
        owner, repo, sha, state, description, _status_target(app["id"])
    )


async def _github_checkout(app: dict, job: dict, sha: str | None = None) -> str | None:
    gh = app.get("github") or {}
    full_name = gh.get("full_name") or ""
    if not full_name:
        return sha
    branch = gh.get("branch") or "main"
    cwd = app["path"]
    token = await github_svc.installation_token()
    code = await _run_git(token, ["fetch", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd, job)
    if code != 0:
        code = await _run_git(token, ["fetch", "--prune", "origin"], cwd, job)
        if code != 0:
            raise RuntimeError(f"git fetch failed with code {code}")
    target = sha or f"origin/{branch}"
    code = await _run_git(token, ["checkout", "-B", branch, target], cwd, job)
    if code != 0:
        raise RuntimeError(f"git checkout {branch} failed with code {code}")
    cap_cmd = ["git", *github_svc.git_config_args(token, cwd), "rev-parse", "HEAD"]
    cap_code, resolved = await _run_capture(cap_cmd, cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    if cap_code == 0 and resolved:
        _log(job, f"HEAD {resolved[:12]}\n")
        return resolved
    return sha


async def _bind_workdir(container: str, host_path: str) -> str | None:
    code, out = await _run_capture(["docker", "inspect", "-f", "{{json .Mounts}}", container], "/")
    if code != 0 or not out:
        return None
    try:
        mounts = json.loads(out)
    except json.JSONDecodeError:
        return None
    want = Path(host_path).as_posix().rstrip("/")
    for mount in mounts or []:
        src = Path(mount.get("Source") or "").as_posix().rstrip("/")
        dest = mount.get("Destination") or ""
        if src == want and dest:
            return dest
    return None


async def _exec_in_app(container: str, workdir: str, args: list[str], job: dict) -> int:
    return await _run_cmd(
        [
            "docker",
            "exec",
            "-w",
            workdir,
            "-e",
            "GIT_CONFIG_COUNT=1",
            "-e",
            "GIT_CONFIG_KEY_0=safe.directory",
            "-e",
            "GIT_CONFIG_VALUE_0=*",
            "-e",
            "COMPOSER_DISABLE_XDEBUG_WARN=1",
            container,
            *args,
        ],
        "/",
        job,
    )


async def _run_app_hooks(app: dict, job: dict) -> None:
    """Install PHP deps and build Vite assets inside bind-mounted app containers.

    Compose --build only rebuilds the PHP image. Frontend files live on the host
    bind mount and public/build is gitignored, so live deploys must run npm build.
    Baked-image apps have no source bind mount and are skipped.
    """
    names = [n for n in (app.get("restart_containers") or []) if n]
    if not names:
        return
    host = compose_cwd(app)
    panel = Path(app["path"])
    container = None
    workdir = None
    for name in names:
        workdir = await _bind_workdir(name, host)
        if workdir:
            container = name
            break
    if not container or not workdir:
        return

    if (panel / "composer.json").is_file():
        code = await _exec_in_app(
            container,
            workdir,
            ["composer", "install", "--no-dev", "--optimize-autoloader", "--no-interaction"],
            job,
        )
        if code != 0:
            if (panel / "vendor" / "autoload.php").is_file():
                _log(
                    job,
                    "composer install failed; keeping existing vendor/ and continuing "
                    "(lockfile may require a newer PHP than this container).\n",
                )
            else:
                raise RuntimeError(f"composer install failed with code {code}")

    package_path = panel / "package.json"
    if package_path.is_file():
        try:
            scripts = (json.loads(package_path.read_text()) or {}).get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        if "build" in scripts:
            install = ["npm", "ci"] if (panel / "package-lock.json").is_file() else ["npm", "install"]
            code = await _exec_in_app(container, workdir, install, job)
            if code != 0 and install == ["npm", "ci"]:
                code = await _exec_in_app(container, workdir, ["npm", "install"], job)
            if code != 0:
                raise RuntimeError(f"npm install failed with code {code}")
            code = await _exec_in_app(container, workdir, ["npm", "run", "build"], job)
            if code != 0:
                raise RuntimeError(f"npm run build failed with code {code}")

    if (panel / "artisan").is_file():
        code = await _exec_in_app(container, workdir, ["php", "artisan", "optimize"], job)
        if code != 0:
            _log(job, f"php artisan optimize exited {code} (continuing)\n")


async def run_deploy(
    app_id: str,
    *,
    do_pull: bool = True,
    do_rebuild: bool = True,
    do_restart: bool = False,
    sha: str | None = None,
    trigger: str = "manual",
) -> dict:
    app = get_app(app_id)
    if not app:
        raise ValueError(f"Unknown app: {app_id}")

    lock = _lock(app_id)
    if lock.locked():
        raise RuntimeError(f"A job is already running for {app_id}")

    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "app_id": app_id,
        "status": "running",
        "trigger": trigger,
        "sha": sha,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "log": "",
        "actions": {"pull": do_pull, "rebuild": do_rebuild, "restart": do_restart},
    }
    _save_job(job)

    async def _work():
        resolved = sha
        async with lock:
            try:
                cwd = app["path"]
                if not Path(cwd).exists():
                    raise FileNotFoundError(f"App path not found: {cwd}")
                if app.get("needs_template") and not app.get("managed"):
                    raise RuntimeError(
                        "This Laravel app has no panel deploy template yet. Open the app and switch it to the panel template."
                    )

                if do_pull and app.get("pull", True):
                    if github_linked(app):
                        resolved = await _github_checkout(app, job, sha)
                        job["sha"] = resolved
                        _save_job(job)
                        await _set_commit_status(app, resolved, "pending", "Deploying on VPS Dashboard")
                    else:
                        code = await _run_cmd(
                            ["git", *github_svc.git_safe_args(cwd), "pull", "--ff-only"],
                            cwd,
                            job,
                        )
                        if code != 0:
                            raise RuntimeError(f"git pull failed with code {code}")

                if do_rebuild:
                    cmd = list(app["rebuild_cmd"])
                    if cmd[:2] == ["docker", "compose"] and app.get("compose_file"):
                        if "-f" not in cmd:
                            cmd = [
                                "docker",
                                "compose",
                                "-f",
                                app["compose_file"],
                                *cmd[2:],
                            ]
                    docker_cwd = compose_cwd(app)
                    cmd = with_compose_env_file(cmd, app, docker_cwd)
                    if docker_cwd != cwd:
                        _log(job, f"compose path {docker_cwd}\n")
                    code = await _run_cmd(cmd, docker_cwd, job)
                    if code != 0:
                        raise RuntimeError(f"rebuild failed with code {code}")

                if do_pull or do_rebuild:
                    await _run_app_hooks(app, job)

                if do_restart and not do_rebuild:
                    for name in app.get("restart_containers", []):
                        code = await _run_cmd(["docker", "restart", name], cwd, job)
                        if code != 0:
                            raise RuntimeError(f"restart {name} failed: {code}")

                job["status"] = "success"
                await _set_commit_status(app, resolved, "success", "Live on VPS Dashboard")
            except Exception as e:
                job["status"] = "failed"
                _log(job, f"\nERROR: {e}\n")
                await _set_commit_status(app, resolved, "failure", str(e)[:140])
            finally:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                _save_job(job)

    asyncio.create_task(_work())
    return job


async def stream_job_log(job_id: str) -> AsyncIterator[str]:
    last = 0
    while True:
        job = get_job(job_id)
        if not job:
            yield 'data: {"error":"not found"}\n\n'
            return
        log = job.get("log", "")
        if len(log) > last:
            chunk = log[last:]
            last = len(log)
            for line in chunk.splitlines(True):
                yield f"data: {line.rstrip()}\n\n"
        if job.get("status") in ("success", "failed"):
            yield f"event: done\ndata: {job['status']}\n\n"
            return
        await asyncio.sleep(0.5)
