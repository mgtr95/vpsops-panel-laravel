"""Panel hostname: env PANEL_DOMAIN plus Settings apply via nginx + certbot."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import certs_svc, proxy_svc, site_svc
from app.utils import read_json, write_json

VHOST_NAME = "vps-dashboard.conf"
UPSTREAM = "http://vps_dashboard:9090"


def _store_path() -> Path:
    return get_settings().panel_path


def load_store() -> dict[str, Any]:
    data = read_json(_store_path(), {}) or {}
    return data if isinstance(data, dict) else {}


def save_store(data: dict[str, Any]) -> dict[str, Any]:
    write_json(_store_path(), data)
    return data


def clear_store() -> None:
    path = _store_path()
    if path.exists():
        path.unlink()


def env_panel_domain() -> str:
    return (get_settings().panel_domain or "").strip().lower()


def stored_domain() -> str:
    return (load_store().get("domain") or "").strip().lower()


def stored_www() -> bool:
    return bool(load_store().get("www"))


def wants_www(domain: str, include_www: bool) -> bool:
    if (domain or "").startswith("www."):
        return False
    return bool(include_www)


def effective_panel_domain() -> str:
    stored = stored_domain()
    if stored:
        return stored
    return env_panel_domain()


def domain_source() -> str:
    if stored_domain():
        return "settings"
    if env_panel_domain():
        return "env"
    return "none"


def normalize_domain(raw: str) -> str:
    host = (raw or "").strip().lower()
    host = host.removeprefix("http://").removeprefix("https://")
    host = host.split("/")[0].split(":")[0].strip().rstrip(".")
    if not host or "." not in host or " " in host:
        raise ValueError("Enter a domain like panel.example.com")
    return host


def _vhost_path() -> Path:
    return site_svc.nginx_conf_dir() / VHOST_NAME


def _proxy_location(forwarded_proto: str) -> str:
    # Variable + Docker DNS so a dashboard recreate does not leave nginx
    # glued to the old container IP (502 No route to host).
    return f"""
    location / {{
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $panel_upstream vps_dashboard;
        proxy_pass http://$panel_upstream:9090;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {forwarded_proto};
        proxy_set_header Connection        "";
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
"""


def _vhost_http(domain: str, *, proxy: bool, include_www: bool) -> str:
    names = domain
    if include_www:
        names = f"{domain} www.{domain}"
    extra = _proxy_location("$scheme") if proxy else """
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


def _vhost_https(domain: str, *, include_www: bool) -> str:
    www_block = ""
    if include_www:
        www_block = f"""
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name www.{domain};

    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    return 301 https://{domain}$request_uri;
}}
"""
    return f"""{_vhost_http(domain, proxy=False, include_www=include_www)}
{www_block}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    client_max_body_size 64m;
{_proxy_location("https")}
}}
"""


def write_panel_vhost(domain: str, *, https: bool, include_www: bool) -> Path:
    conf_dir = site_svc.nginx_conf_dir()
    conf_dir.mkdir(parents=True, exist_ok=True)
    if not conf_dir.is_dir():
        raise RuntimeError(
            "Website config folder is missing. Open Websites and start hosting first."
        )
    path = conf_dir / VHOST_NAME
    www = wants_www(domain, include_www)
    text = _vhost_https(domain, include_www=www) if https else _vhost_http(
        domain, proxy=True, include_www=www
    )
    path.write_text(text)
    return path


def _require_hosting() -> None:
    if not proxy_svc.is_ready():
        raise RuntimeError(
            "Website hosting is not set up yet. Open Websites, add an email, and start hosting first."
        )
    if not proxy_svc.acme_email():
        raise RuntimeError(
            "Add an email on the Websites page so Let's Encrypt can issue a certificate."
        )


def _require_reload(reload: dict) -> dict:
    if not reload.get("ok"):
        raise RuntimeError(reload.get("error") or reload.get("log") or "Could not reload websites.")
    return reload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist(domain: str, https: bool, include_www: bool) -> dict[str, Any]:
    _health_cache["at"] = 0.0
    _health_cache["value"] = None
    return save_store(
        {
            "domain": domain,
            "https": https,
            "www": wants_www(domain, include_www),
            "applied_at": _now(),
        }
    )


_HEALTH_TTL_SEC = 30
_health_cache: dict[str, Any] = {"at": 0.0, "value": None}


def domain_health(force: bool = False) -> dict[str, Any]:
    """Whether the applied panel domain is still usable (DNS + unexpired cert)."""
    now = time.monotonic()
    cached = _health_cache.get("value")
    if not force and cached is not None and now - float(_health_cache.get("at") or 0) < _HEALTH_TTL_SEC:
        return cached

    domain = effective_panel_domain()
    result: dict[str, Any] = {
        "domain": domain or None,
        "ok": False,
        "reason": "none",
        "redirect_to": None,
    }
    if not domain:
        _health_cache["at"] = now
        _health_cache["value"] = result
        return result

    dns = site_svc.check_dns(domain, include_www=False)
    if not dns.get("ok"):
        result["reason"] = "dns"
        _health_cache["at"] = now
        _health_cache["value"] = result
        return result

    info = certs_svc.cert_info(domain)
    auto_https = _auto_https_from_env()
    if info:
        if info.get("expired"):
            result["reason"] = "cert"
            _health_cache["at"] = now
            _health_cache["value"] = result
            return result
    elif not auto_https:
        result["reason"] = "cert"
        _health_cache["at"] = now
        _health_cache["value"] = result
        return result

    result["ok"] = True
    result["reason"] = None
    result["redirect_to"] = f"https://{domain}"
    _health_cache["at"] = now
    _health_cache["value"] = result
    return result


def _auto_https_from_env() -> bool:
    auto = os.environ.get("PANEL_AUTO_HTTPS", "").lower()
    domain = env_panel_domain()
    if not auto:
        return bool(domain)
    return auto in ("1", "true", "yes")


def access_info() -> dict[str, Any]:
    stored = load_store()
    domain = effective_panel_domain()
    env_domain = env_panel_domain() or None
    source = domain_source()
    https_ready = bool(domain) and (
        bool(stored.get("https")) or (bool(domain) and site_svc.cert_exists(domain))
    )
    auto_https = _auto_https_from_env() and source != "settings"
    port = os.environ.get("PANEL_PORT", "9090")
    public_ip = site_svc.public_ip()
    http_ip_url = f"http://{public_ip or 'SERVER_IP'}:{port}"
    websites_ready = proxy_svc.is_ready()
    acme_email_set = bool(proxy_svc.acme_email())

    if domain and (https_ready or auto_https):
        mode = "https-domain"
        primary_url = f"https://{domain}"
    elif domain:
        mode = "http-behind-proxy"
        primary_url = f"https://{domain}"
    else:
        mode = "http-ip"
        primary_url = http_ip_url

    health = domain_health()

    return {
        "mode": mode,
        "panel_domain": domain or None,
        "effective_domain": domain or None,
        "env_domain": env_domain,
        "source": source,
        "auto_https": bool(auto_https and domain),
        "https_ready": bool(https_ready or (auto_https and domain)),
        "domain_ok": bool(health.get("ok")),
        "domain_issue": health.get("reason"),
        "redirect_to": health.get("redirect_to"),
        "www": stored_www() if source == "settings" else False,
        "websites_ready": websites_ready,
        "acme_email_set": acme_email_set,
        "primary_url": primary_url,
        "http_ip_url": http_ip_url,
        "port": port,
        "hint": {
            "http-ip": "No domain set — use the VPS IP on port 9090, or paste a domain on Settings.",
            "https-domain": "Panel is on this domain with HTTPS. Visiting the IP redirects there while DNS and the certificate are healthy.",
            "http-behind-proxy": "Domain is set but HTTPS is not ready yet. Apply on Settings after DNS points here.",
        }.get(mode),
    }


def check_panel_dns(domain: str, include_www: bool = True) -> dict[str, Any]:
    host = normalize_domain(domain)
    return site_svc.check_dns(host, include_www=wants_www(host, include_www))


async def apply_domain(raw: str, include_www: bool = True) -> dict[str, Any]:
    domain = normalize_domain(raw)
    www = wants_www(domain, include_www)
    _require_hosting()
    proxy_svc.ensure_dashboard_on_network()
    dns = site_svc.check_dns(domain, include_www=www)

    previous = stored_domain()
    if previous and previous != domain:
        old = _vhost_path()
        if old.exists():
            old.unlink()

    https = site_svc.cert_exists(domain)
    write_panel_vhost(domain, https=https, include_www=www)
    reload = _require_reload(await certs_svc.reload_nginx())

    if not dns["ok"]:
        _persist(domain, https=https, include_www=www)
        return {
            "ok": False,
            "waiting_dns": True,
            "domain": domain,
            "www": www,
            "dns": dns,
            "nginx": reload,
            "message": "Point the domain (and www, if checked) at this server first, then click Check DNS / Apply again.",
            **access_info(),
        }

    cert = {"ok": True, "existing": True}
    if (not https) or www:
        cert = await site_svc.issue_certificate(domain, include_www=www)
    write_panel_vhost(domain, https=True, include_www=www)
    reload = _require_reload(await certs_svc.reload_nginx())
    _persist(domain, https=True, include_www=www)
    return {
        "ok": True,
        "domain": domain,
        "www": www,
        "dns": dns,
        "certificate": cert,
        "nginx": reload,
        **access_info(),
    }


async def clear_domain() -> dict[str, Any]:
    path = _vhost_path()
    if path.exists():
        path.unlink()
    if proxy_svc.find_nginx_container() is not None:
        _require_reload(await certs_svc.reload_nginx())
    clear_store()
    _health_cache["at"] = 0.0
    _health_cache["value"] = None
    return {"ok": True, "message": "Panel domain removed. Use the VPS IP on port 9090.", **access_info()}
