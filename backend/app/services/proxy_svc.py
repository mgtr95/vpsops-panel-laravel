"""Detect or provision nginx + certbot + the shared proxy Docker network."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path
from typing import Any

from docker.errors import APIError, NotFound

from app.config import get_settings
from app.services import docker_svc
from app.utils import read_json, write_json

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "proxy"

MANAGED_NGINX = "vps_nginx"
MANAGED_CERTBOT = "vps_certbot"
MANAGED_NETWORK = "vps_proxy"
DASHBOARD_NAME = "vps_dashboard"
SKIP_NETWORKS = {"bridge", "host", "none"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _store_path() -> Path:
    return get_settings().proxy_path


def load_store() -> dict[str, Any]:
    data = read_json(_store_path(), {}) or {}
    return data if isinstance(data, dict) else {}


def save_store(data: dict[str, Any]) -> dict[str, Any]:
    write_json(_store_path(), data)
    return data


def _merge_store(updates: dict[str, Any]) -> dict[str, Any]:
    current = load_store()
    current.update({k: v for k, v in updates.items() if v not in (None, "")})
    return save_store(current)


def _running(container) -> bool:
    return container is not None and container.status == "running"


def _get_named(name: str | None):
    if not name or not str(name).strip():
        return None
    try:
        return docker_svc.get_client().containers.get(name.strip())
    except Exception:
        return None


def _is_nginx(container) -> bool:
    name = (container.name or "").lower()
    image = ((container.attrs.get("Config") or {}).get("Image") or "").lower()
    if "certbot" in name or "certbot" in image:
        return False
    return "nginx" in name or "nginx" in image


def _is_certbot(container) -> bool:
    name = (container.name or "").lower()
    image = ((container.attrs.get("Config") or {}).get("Image") or "").lower()
    return "certbot" in name or "certbot" in image


def find_nginx_container():
    stored = load_store()
    settings = get_settings()
    for name in (
        stored.get("nginx_container"),
        settings.nginx_container,
        MANAGED_NGINX,
        "nginx",
    ):
        c = _get_named(name)
        if _running(c) and _is_nginx(c):
            return c
    for c in docker_svc.get_client().containers.list():
        if _is_nginx(c):
            return c
    return None


def find_certbot_container():
    stored = load_store()
    settings = get_settings()
    for name in (
        stored.get("certbot_container"),
        settings.certbot_container,
        MANAGED_CERTBOT,
        "certbot",
    ):
        c = _get_named(name)
        if _running(c) and _is_certbot(c):
            return c
    for c in docker_svc.get_client().containers.list():
        if _is_certbot(c):
            return c
    return None


def _container_networks(container) -> list[str]:
    if container is None:
        return []
    nets = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    return [n for n in nets if n not in SKIP_NETWORKS]


def _guess_conf_dir(nginx) -> Path:
    stored = (load_store().get("conf_dir") or "").strip()
    if stored:
        path = Path(stored)
        if path.is_dir():
            return path
    settings = get_settings()
    if settings.nginx_conf_dir.strip():
        path = Path(settings.nginx_conf_dir.strip())
        if path.is_dir():
            return path
    default = Path(settings.apps_root) / "nginx" / "conf.d"
    if nginx is not None:
        for mount in nginx.attrs.get("Mounts") or []:
            dest = mount.get("Destination") or ""
            if dest.rstrip("/") in ("/etc/nginx/conf.d", "/etc/nginx/conf"):
                if default.is_dir():
                    return default
    if default.is_dir():
        return default
    return default


def _guess_letsencrypt_path() -> Path:
    stored = (load_store().get("letsencrypt_path") or "").strip()
    if stored and Path(stored).exists():
        return Path(stored)
    settings = get_settings()
    managed = Path(settings.apps_root) / "nginx" / "letsencrypt"
    if (managed / "live").exists():
        return managed
    env_path = Path(settings.letsencrypt_path or "/etc/letsencrypt")
    if (env_path / "live").exists() or env_path.exists():
        return env_path
    return managed if load_store().get("managed") else env_path


def _guess_network(nginx) -> str:
    stored = (load_store().get("network") or "").strip()
    if stored:
        return stored
    settings = get_settings()
    if settings.proxy_network.strip():
        return settings.proxy_network.strip()
    nets = _container_networks(nginx)
    if MANAGED_NETWORK in nets:
        return MANAGED_NETWORK
    if nets:
        return nets[0]
    return MANAGED_NETWORK


def compose_dir() -> Path:
    stored = (load_store().get("compose_dir") or "").strip()
    if stored:
        return Path(stored)
    return Path(get_settings().apps_root) / "nginx"


def conf_dir() -> Path:
    return _guess_conf_dir(find_nginx_container())


def letsencrypt_path() -> Path:
    return _guess_letsencrypt_path()


def network_name() -> str:
    return _guess_network(find_nginx_container())


def acme_email() -> str:
    stored = (load_store().get("acme_email") or "").strip()
    if stored:
        return stored
    return (get_settings().acme_email or "").strip()


def nginx_container_name() -> str | None:
    c = find_nginx_container()
    return c.name if c is not None else None


def certbot_container_name() -> str | None:
    c = find_certbot_container()
    return c.name if c is not None else None


def is_ready() -> bool:
    nginx = find_nginx_container()
    certbot = find_certbot_container()
    conf = _guess_conf_dir(nginx)
    return _running(nginx) and _running(certbot) and conf.is_dir() and _writable(conf)


def _writable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        probe = path / ".panel-write-test"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def ports_in_use() -> dict[int, str | None]:
    used: dict[int, str | None] = {80: None, 443: None}
    client = docker_svc.get_client()
    for c in client.containers.list():
        bindings = (c.attrs.get("HostConfig") or {}).get("PortBindings") or {}
        for key, binds in bindings.items():
            if not binds:
                continue
            for bind in binds:
                host_port = bind.get("HostPort")
                if host_port in ("80", "443"):
                    used[int(host_port)] = c.name
    return used


def _dashboard_container():
    client = docker_svc.get_client()
    for name in (socket.gethostname(), DASHBOARD_NAME, "dashboard"):
        c = _get_named(name)
        if c is not None:
            return c
    try:
        me = client.containers.get(socket.gethostname())
        return me
    except Exception:
        return None


def _connect_to_network(container, net_name: str) -> None:
    if container is None:
        return
    client = docker_svc.get_client()
    try:
        client.networks.get(net_name).connect(container)
    except APIError as e:
        if "already" not in str(e).lower():
            raise
    except Exception:
        return


def ensure_network(name: str | None = None) -> str:
    net_name = (name or network_name() or MANAGED_NETWORK).strip() or MANAGED_NETWORK
    client = docker_svc.get_client()
    try:
        client.networks.get(net_name)
    except NotFound:
        client.networks.create(net_name, driver="bridge", check_duplicate=True)
    _connect_to_network(find_nginx_container(), net_name)
    return net_name


def connect_dashboard(net_name: str | None = None) -> dict[str, Any]:
    name = ensure_network(net_name)
    dash = _dashboard_container()
    if dash is None:
        return {"ok": False, "error": "Could not find the dashboard container to join the website network."}
    client = docker_svc.get_client()
    try:
        net = client.networks.get(name)
        net.connect(dash)
        return {"ok": True, "network": name, "container": dash.name, "already": False}
    except APIError as e:
        msg = str(e).lower()
        if "already" in msg:
            return {"ok": True, "network": name, "container": dash.name, "already": True}
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def connect_dashboard_to_peer(container_name: str | None) -> None:
    """Join the dashboard to a DB container's networks so hostname DNS works."""
    host = (container_name or "").strip()
    if not host or host.lower() in {"127.0.0.1", "localhost", "::1"}:
        return
    dash = _dashboard_container()
    if dash is None:
        return
    peer = _get_named(host)
    if peer is None:
        return
    client = docker_svc.get_client()
    for net_name in _container_networks(peer):
        try:
            client.networks.get(net_name).connect(dash)
        except APIError as e:
            if "already" not in str(e).lower():
                continue
        except Exception:
            continue


def ensure_dashboard_on_network() -> None:
    nginx = find_nginx_container()
    if nginx is None:
        return
    try:
        connect_dashboard(_guess_network(nginx))
    except Exception:
        pass


def remember_detected() -> dict[str, Any]:
    nginx = find_nginx_container()
    certbot = find_certbot_container()
    if nginx is None:
        return load_store()
    updates = {
        "nginx_container": nginx.name,
        "certbot_container": certbot.name if certbot is not None else "",
        "conf_dir": str(_guess_conf_dir(nginx)),
        "network": _guess_network(nginx),
        "letsencrypt_path": str(_guess_letsencrypt_path()),
        "compose_dir": str(compose_dir()),
    }
    if "managed" not in load_store():
        updates["managed"] = nginx.name == MANAGED_NGINX
    return _merge_store(updates)


def status() -> dict[str, Any]:
    from app.services.site_svc import public_ip

    nginx = find_nginx_container()
    certbot = find_certbot_container()
    conf = _guess_conf_dir(nginx)
    ports = ports_in_use()
    nginx_name = nginx.name if nginx is not None else None
    busy = {
        str(port): owner
        for port, owner in ports.items()
        if owner and owner != nginx_name
    }
    ready = is_ready()
    if ready:
        remember_detected()
    store = load_store()
    return {
        "ready": ready,
        "managed": bool(store.get("managed")),
        "nginx": {
            "name": nginx_name,
            "running": _running(nginx),
        },
        "certbot": {
            "name": certbot.name if certbot is not None else None,
            "running": _running(certbot),
        },
        "conf_dir": str(conf),
        "conf_dir_exists": conf.is_dir(),
        "conf_dir_writable": _writable(conf) if conf.is_dir() else False,
        "network": _guess_network(nginx),
        "letsencrypt_path": str(_guess_letsencrypt_path()),
        "acme_email": acme_email(),
        "public_ip": public_ip(),
        "ports": {
            "80": ports.get(80),
            "443": ports.get(443),
        },
        "ports_blocked": busy,
        "compose_dir": str(compose_dir()),
        "compose_exists": (compose_dir() / "docker-compose.yml").is_file(),
    }


def _run_compose(directory: Path) -> str:
    proc = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=str(directory),
        capture_output=True,
        text=True,
        timeout=180,
    )
    log = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(log or f"Could not start website hosting (exit {proc.returncode}).")
    return log


def _write_managed_stack(root: Path) -> None:
    (root / "conf.d").mkdir(parents=True, exist_ok=True)
    (root / "certbot-www").mkdir(parents=True, exist_ok=True)
    (root / "letsencrypt").mkdir(parents=True, exist_ok=True)
    compose_src = (TEMPLATES / "docker-compose.yml").read_text()
    acme_src = (TEMPLATES / "00-acme.conf").read_text()
    (root / "docker-compose.yml").write_text(compose_src)
    acme_path = root / "conf.d" / "00-acme.conf"
    if not acme_path.exists():
        acme_path.write_text(acme_src)


def setup(email: str) -> dict[str, Any]:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a real email address. Let's Encrypt uses it for certificate notices.")
    if is_ready():
        _merge_store({"acme_email": email})
        ensure_dashboard_on_network()
        return {"ok": True, "already": True, "status": status(), "log": "Website hosting is already running."}

    steps: list[str] = []
    nginx = find_nginx_container()
    ports = ports_in_use()
    blockers = []
    for port, owner in ports.items():
        if owner and (nginx is None or owner != nginx.name):
            blockers.append(f"port {port} is used by {owner}")
    if blockers:
        raise RuntimeError(
            "This server cannot start website hosting yet: "
            + "; ".join(blockers)
            + ". Open Docker, stop that container, then try again."
        )

    steps.append("Creating the private website network…")
    net = ensure_network(MANAGED_NETWORK)
    steps.append(f"Network ready ({net}).")

    root = compose_dir()
    compose_file = root / "docker-compose.yml"
    if compose_file.is_file():
        steps.append("Found existing hosting files. Starting them without changing them…")
        log = _run_compose(root)
        managed = False
    else:
        steps.append("Writing website hosting files…")
        root.mkdir(parents=True, exist_ok=True)
        _write_managed_stack(root)
        steps.append("Starting nginx and certificate helper…")
        log = _run_compose(root)
        managed = True

    nginx = find_nginx_container()
    certbot = find_certbot_container()
    if not _running(nginx):
        raise RuntimeError(
            "Hosting started but nginx is not running. Check Docker for errors.\n" + log
        )
    if not _running(certbot):
        raise RuntimeError(
            "Hosting started but the certificate helper is not running. Check Docker for errors.\n" + log
        )

    steps.append("Connecting this panel to the website network…")
    joined = connect_dashboard(_guess_network(nginx) if not managed else MANAGED_NETWORK)
    if not joined.get("ok"):
        raise RuntimeError(joined.get("error") or "Could not join the website network.")

    save_store(
        {
            "managed": managed,
            "nginx_container": nginx.name,
            "certbot_container": certbot.name if certbot is not None else "",
            "conf_dir": str(_guess_conf_dir(nginx)),
            "network": MANAGED_NETWORK if managed else _guess_network(nginx),
            "letsencrypt_path": str(_guess_letsencrypt_path()),
            "compose_dir": str(root),
            "acme_email": email,
        }
    )
    steps.append("Website hosting is on. You can add a domain to an app next.")
    return {
        "ok": True,
        "already": False,
        "managed": managed,
        "steps": steps,
        "log": "\n".join(steps) + ("\n" + log if log else ""),
        "status": status(),
    }
