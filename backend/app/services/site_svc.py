"""Domain DNS checks, nginx vhosts, and Let's Encrypt via the existing certbot container."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx

from app.services import certs_svc, proxy_svc
from app.services.deploy_svc import get_app, upsert_app
from app.utils import parse_dotenv, write_dotenv


def public_ip() -> str | None:
    try:
        with httpx.Client(timeout=8) as client:
            r = client.get("https://api.ipify.org")
            if r.status_code == 200 and r.text.strip():
                return r.text.strip()
    except Exception:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("1.1.1.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def resolve_domain(domain: str) -> list[str]:
    host = domain.strip().rstrip(".").lower()
    if not host:
        return []
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            ips.add(info[4][0])
    except socket.gaierror:
        return []
    return sorted(ips)


def check_dns(domain: str, include_www: bool = True) -> dict:
    ip = public_ip()
    names = [domain.strip().lower()]
    if include_www and not domain.lower().startswith("www."):
        names.append(f"www.{names[0]}")
    records = []
    all_ok = True
    for name in names:
        resolved = resolve_domain(name)
        ok = bool(ip) and ip in resolved
        if not resolved or not ok:
            all_ok = False
        records.append({"host": name, "resolved": resolved, "ok": ok})
    return {
        "domain": domain.strip().lower(),
        "public_ip": ip,
        "ok": all_ok,
        "records": records,
        "instructions": dns_instructions(ip),
    }


def dns_instructions(ip: str | None) -> dict:
    target = ip or "YOUR_SERVER_IP"
    return {
        "summary": f"At your domain registrar, create an A record pointing to {target}.",
        "steps": [
            "Open the DNS settings for this domain (GoDaddy, Namecheap, Cloudflare, or whoever you bought it from).",
            f"Add an A record: host @ (or blank) → {target}",
            f"Optional: add an A record: host www → {target}",
            "Save. DNS can take a few minutes (sometimes up to an hour).",
        ],
        "ip": target,
    }


def nginx_conf_dir() -> Path:
    return proxy_svc.conf_dir()


def nginx_available() -> dict:
    nginx = proxy_svc.find_nginx_container()
    certbot = proxy_svc.find_certbot_container()
    conf = proxy_svc.conf_dir()
    return {
        "ok": proxy_svc.is_ready(),
        "conf_dir": str(conf),
        "conf_dir_exists": conf.is_dir(),
        "container": nginx.name if nginx is not None else None,
        "expected_container": nginx.name if nginx is not None else None,
        "certbot": certbot.name if certbot is not None else None,
        "network": proxy_svc.network_name(),
    }


def _nginx_container():
    return proxy_svc.find_nginx_container()


def _proxy_location(slug: str) -> str:
    # Variable + Docker DNS so nginx -t succeeds before the app container exists.
    return f"""
    location / {{
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $panel_upstream {slug}_app;
        proxy_pass http://$panel_upstream:80;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";
        proxy_read_timeout 60s;
        proxy_buffer_size 128k;
        proxy_buffers 8 128k;
        proxy_busy_buffers_size 256k;
    }}
"""


def _vhost_http(domain: str, slug: str, include_www: bool, proxy: bool) -> str:
    names = domain
    if include_www and not domain.startswith("www."):
        names = f"{domain} www.{domain}"
    extra = _proxy_location(slug) if proxy else """
    location / {
        return 301 https://$host$request_uri;
    }
"""
    return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {names};

    location /.well-known/acme-challenge/ {{
        root /var/www/certbot;
    }}
{extra}
}}
"""


def _vhost_https(domain: str, slug: str, include_www: bool, client_max_body_size_mb: int = 32) -> str:
    www_block = ""
    if include_www and not domain.startswith("www."):
        www_block = f"""
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name www.{domain};

    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    return 301 https://{domain}$request_uri;
}}
"""
    return f"""{_vhost_http(domain, slug, include_www, proxy=False)}
{www_block}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    client_max_body_size {int(client_max_body_size_mb)}m;
{_proxy_location(slug)}
}}
"""


def write_vhost(app: dict, https: bool) -> Path:
    from app.services import laravel_svc

    conf_dir = nginx_conf_dir()
    conf_dir.mkdir(parents=True, exist_ok=True)
    if not conf_dir.is_dir():
        raise RuntimeError(
            "Website config folder is missing. Open Websites and start hosting first."
        )
    slug = app["id"]
    domain = (app.get("domain") or "").strip().lower()
    include_www = bool(app.get("www", True))
    body_mb = laravel_svc.normalize_php(app.get("php"))["client_max_body_size_mb"]
    text = (
        _vhost_https(domain, slug, include_www, body_mb)
        if https
        else _vhost_http(domain, slug, include_www, proxy=True)
    )
    path = conf_dir / f"{slug}.conf"
    path.write_text(text)
    return path


async def refresh_vhost_body_size(app: dict) -> dict | None:
    """Rewrite HTTPS vhost when the app already has a live domain conf. Returns nginx reload result or None."""
    domain = (app.get("domain") or "").strip().lower()
    if not domain:
        return None
    conf = nginx_conf_dir() / f"{app['id']}.conf"
    if not conf.is_file():
        return None
    if not nginx_available().get("ok"):
        return None
    write_vhost(app, https=cert_exists(domain))
    return _require_reload(await certs_svc.reload_nginx())


def cert_exists(domain: str) -> bool:
    live = proxy_svc.letsencrypt_path() / "live" / domain / "fullchain.pem"
    return live.exists()


async def issue_certificate(domain: str, include_www: bool) -> dict:
    email = proxy_svc.acme_email()
    if not email:
        raise RuntimeError("Add an email on the Websites page so Let's Encrypt can issue certificates.")
    container = proxy_svc.find_certbot_container()
    if container is None:
        raise RuntimeError("Certificate helper is not running. Open Websites and start hosting first.")
    args = [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        "/var/www/certbot",
        "-d",
        domain,
        "--non-interactive",
        "--agree-tos",
        "-m",
        email,
        "--keep-until-expiring",
    ]
    if include_www and not domain.startswith("www."):
        args.extend(["-d", f"www.{domain}", "--expand"])
    result = await asyncio.to_thread(container.exec_run, args, demux=True)
    out = ""
    if result.output:
        stdout, stderr = result.output
        out = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
    if result.exit_code != 0:
        raise RuntimeError(out.strip() or f"certbot failed with code {result.exit_code}")
    return {"ok": True, "log": out}


async def attach_domain(app_id: str, domain: str, include_www: bool = True) -> dict:
    app = get_app(app_id)
    if not app:
        raise ValueError(f"Unknown app: {app_id}")
    domain = domain.strip().lower().removeprefix("http://").removeprefix("https://").split("/")[0]
    if not domain or "." not in domain:
        raise ValueError("Enter a domain like example.com")
    nginx = nginx_available()
    if not nginx["ok"]:
        raise RuntimeError(
            "Website hosting is not set up yet. Open Websites and start hosting, then try Go live again."
        )
    dns = check_dns(domain, include_www)
    app["domain"] = domain
    app["www"] = include_www
    upsert_app(app)
    proxy_svc.ensure_network()

    env_path = Path(app["path"]) / ".env"
    if env_path.exists():
        values = parse_dotenv(env_path)
        values["APP_URL"] = f"https://{domain}"
        asset = (values.get("ASSET_URL") or "").strip()
        if not asset.startswith("https://"):
            values["ASSET_URL"] = values["APP_URL"]
        write_dotenv(env_path, values, Path(app["path"]) / ".env.example")

    https = cert_exists(domain)
    write_vhost(app, https=https)
    reload = _require_reload(await certs_svc.reload_nginx())
    if not https:
        if not dns["ok"]:
            return {
                "ok": False,
                "waiting_dns": True,
                "dns": dns,
                "nginx": reload,
                "message": "Point the domain at this server first, then click Check DNS / Go live again.",
            }
        cert = await issue_certificate(domain, include_www)
        write_vhost(app, https=True)
        reload = _require_reload(await certs_svc.reload_nginx())
        return {"ok": True, "dns": dns, "certificate": cert, "nginx": reload}
    return {"ok": True, "dns": dns, "certificate": {"ok": True, "existing": True}, "nginx": reload}


def _require_reload(reload: dict) -> dict:
    if not reload.get("ok"):
        raise RuntimeError(reload.get("error") or reload.get("log") or "Could not reload websites.")
    return reload
