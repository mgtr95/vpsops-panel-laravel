from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import APIError, NotFound

from app.config import get_settings
from app.services.container_status import explain_container, health_check_history
from app.utils import redact_env


def get_client() -> docker.DockerClient:
    return docker.from_env()


def _container_summary(c) -> dict:
    attrs = c.attrs
    state = attrs.get("State", {})
    health = (state.get("Health") or {}).get("Status")
    ports = attrs.get("NetworkSettings", {}).get("Ports") or {}
    port_list = []
    for container_port, bindings in ports.items():
        if not bindings:
            port_list.append({"container": container_port, "host": None})
            continue
        for b in bindings:
            host = f"{b.get('HostIp', '0.0.0.0')}:{b.get('HostPort')}"
            port_list.append({"container": container_port, "host": host})
    networks = list((attrs.get("NetworkSettings", {}).get("Networks") or {}).keys())
    labels = attrs.get("Config", {}).get("Labels") or {}
    config = attrs.get("Config") or {}
    return {
        "id": c.short_id,
        "name": c.name,
        "image": config.get("Image"),
        "status": c.status,
        "state": state.get("Status"),
        "health": health,
        "created": attrs.get("Created"),
        "ports": port_list,
        "networks": networks,
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "restart_count": int(attrs.get("RestartCount") or 0),
        "exit_code": state.get("ExitCode"),
        "cmd": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "issue": explain_container(attrs),
    }


def list_containers(all_containers: bool = True) -> list[dict]:
    client = get_client()
    return [_container_summary(c) for c in client.containers.list(all=all_containers)]


def get_container(name_or_id: str):
    client = get_client()
    return client.containers.get(name_or_id)


def container_action(name_or_id: str, action: str) -> dict:
    c = get_container(name_or_id)
    if action == "start":
        c.start()
    elif action == "stop":
        c.stop()
    elif action == "restart":
        c.restart()
    elif action == "remove":
        c.remove(force=True)
    else:
        raise ValueError(f"Unknown action: {action}")
    if action != "remove":
        c.reload()
        return _container_summary(c)
    return {"ok": True, "removed": name_or_id}


def container_logs(name_or_id: str, tail: int = 200) -> str:
    c = get_container(name_or_id)
    raw = c.logs(tail=tail, timestamps=True)
    return raw.decode("utf-8", errors="replace")


def container_inspect(name_or_id: str) -> dict:
    c = get_container(name_or_id)
    attrs = c.attrs
    config = attrs.get("Config", {})
    return {
        "id": attrs.get("Id"),
        "name": c.name,
        "image": config.get("Image"),
        "status": c.status,
        "created": attrs.get("Created"),
        "started": attrs.get("State", {}).get("StartedAt"),
        "finished": attrs.get("State", {}).get("FinishedAt"),
        "health": (attrs.get("State", {}).get("Health") or {}).get("Status"),
        "issue": explain_container(attrs),
        "health_checks": health_check_history(attrs),
        "env": redact_env(config.get("Env")),
        "cmd": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "mounts": [
            {
                "source": m.get("Source"),
                "destination": m.get("Destination"),
                "mode": m.get("Mode"),
                "type": m.get("Type"),
            }
            for m in attrs.get("Mounts", [])
        ],
        "networks": list(
            (attrs.get("NetworkSettings", {}).get("Networks") or {}).keys()
        ),
        "ports": attrs.get("NetworkSettings", {}).get("Ports"),
        "labels": config.get("Labels") or {},
        "restart_policy": attrs.get("HostConfig", {}).get("RestartPolicy"),
    }


def list_images() -> list[dict]:
    client = get_client()
    images = []
    for img in client.images.list():
        tags = img.tags or ["<none>"]
        images.append(
            {
                "id": img.short_id,
                "tags": tags,
                "size": img.attrs.get("Size"),
                "created": img.attrs.get("Created"),
            }
        )
    return images


def prune_images() -> dict:
    client = get_client()
    return client.images.prune(filters={"dangling": True})


def prune_builder() -> dict:
    client = get_client()
    # builder prune via API
    try:
        return client.api.prune_builds()
    except Exception as e:
        return {"error": str(e)}


def system_df() -> dict:
    client = get_client()
    return client.df()


def disk_usage() -> dict:
    total, used, free = shutil.disk_usage("/")
    return {
        "total": total,
        "used": used,
        "free": free,
        "percent": round(used / total * 100, 1) if total else 0,
    }


def discover_compose_projects() -> list[dict]:
    settings = get_settings()
    roots = settings.scan_paths()
    found = []
    for root in roots:
        if not root.exists():
            continue
        for path in list(root.rglob("docker-compose*.yml")) + list(
            root.rglob("docker-compose*.yaml")
        ):
            if any(part in {"node_modules", ".git", "vendor", "vendor-bin"} for part in path.parts):
                continue
            found.append(
                {
                    "path": str(path),
                    "dir": str(path.parent),
                    "name": path.parent.name,
                    "file": path.name,
                }
            )
    seen = set()
    unique = []
    for item in found:
        key = item["path"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return sorted(unique, key=lambda x: x["path"])


def overview() -> dict:
    containers = list_containers(True)
    running = [c for c in containers if c["status"] == "running"]
    unhealthy = [
        c
        for c in containers
        if c.get("health") == "unhealthy" or c["status"] not in ("running", "created")
    ]
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "containers_total": len(containers),
        "containers_running": len(running),
        "unhealthy": unhealthy,
        "disk": disk_usage(),
        "docker_df": system_df(),
    }
