from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from app.services import proxy_svc


def _parse_cert(path: Path) -> dict | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    cert = x509.load_pem_x509_certificate(data, default_backend())
    sans = []
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans = ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass

    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)
    days_left = (not_after - now).days

    subject = cert.subject.rfc4514_string()
    issuer = cert.issuer.rfc4514_string()
    cn = None
    for attr in cert.subject:
        if attr.oid == x509.oid.NameOID.COMMON_NAME:
            cn = attr.value
            break

    return {
        "subject": subject,
        "cn": cn,
        "issuer": issuer,
        "sans": sans,
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "days_left": days_left,
        "expired": days_left < 0,
        "path": str(path),
    }


def cert_info(domain: str) -> dict | None:
    host = (domain or "").strip().lower()
    if not host:
        return None
    return _parse_cert(proxy_svc.letsencrypt_path() / "live" / host / "fullchain.pem")


def list_certificates() -> list[dict]:
    live = proxy_svc.letsencrypt_path() / "live"
    if not live.exists():
        return []
    certs = []
    for domain_dir in sorted(live.iterdir()):
        if not domain_dir.is_dir() or domain_dir.name.startswith("."):
            continue
        fullchain = domain_dir / "fullchain.pem"
        info = _parse_cert(fullchain)
        if not info:
            continue
        info["domain"] = domain_dir.name
        info["files"] = [
            p.name for p in domain_dir.iterdir() if p.is_file() and not p.name.startswith(".")
        ]
        certs.append(info)
    return certs


def _served_cert_enddate(domain: str) -> str | None:
    try:
        # openssl s_client
        proc = subprocess.run(
            [
                "openssl",
                "s_client",
                "-connect",
                f"{domain}:443",
                "-servername",
                domain,
            ],
            input=b"",
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0 and not proc.stdout:
            return None
        # Extract cert and get enddate
        proc2 = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate"],
            input=proc.stdout,
            capture_output=True,
            timeout=10,
        )
        out = proc2.stdout.decode().strip()
        return out or None
    except Exception:
        return None


def check_served(domains: list[str] | None = None) -> list[dict]:
    certs = list_certificates()
    if domains is None:
        domains = [c["domain"] for c in certs]
    results = []
    for domain in domains:
        disk_info = next((c for c in certs if c["domain"] == domain), None)
        served = _served_cert_enddate(domain)
        disk_end = None
        if disk_info:
            disk_end = f"notAfter={datetime.fromisoformat(disk_info['not_after']).strftime('%b %d %H:%M:%S %Y GMT')}"
            # openssl enddate format differs; compare via parsing served if possible
        match = None
        if served and disk_info:
            # Compare by days_left approx: parse served notAfter
            try:
                # notAfter=Mar 15 12:00:00 2026 GMT
                raw = served.split("=", 1)[1].strip()
                served_dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc
                )
                disk_dt = datetime.fromisoformat(disk_info["not_after"])
                match = abs((served_dt - disk_dt).total_seconds()) < 120
            except Exception:
                match = None
        results.append(
            {
                "domain": domain,
                "disk": disk_info,
                "served_enddate": served,
                "match": match,
            }
        )
    return results


async def renew_certificates() -> dict:
    container = proxy_svc.find_certbot_container()
    if container is None:
        result = {"ok": False, "error": "Certificate helper is not running. Open Websites and start hosting first."}
        from app.services import cert_alerts_svc

        await cert_alerts_svc.notify_renewal(result)
        return result

    result_exec = container.exec_run(
        ["certbot", "renew", "--webroot", "-w", "/var/www/certbot"],
        demux=True,
    )
    out = ""
    if result_exec.output:
        stdout, stderr = result_exec.output
        out = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(
            errors="replace"
        )
    reload = {"ok": True}
    if result_exec.exit_code == 0:
        reload = await reload_nginx()
    result = {
        "ok": result_exec.exit_code == 0 and reload.get("ok", False),
        "exit_code": result_exec.exit_code,
        "log": out,
        "nginx": reload,
    }
    if not result["ok"] and result_exec.exit_code != 0:
        result["error"] = "Certificate renewal failed."
    elif not result["ok"]:
        result["error"] = reload.get("error") or "Nginx reload failed after renewal."

    from app.services import cert_alerts_svc

    await cert_alerts_svc.notify_renewal(result)
    return result


def _exec_output(result) -> str:
    if not result.output:
        return ""
    if isinstance(result.output, tuple):
        stdout, stderr = result.output
        return (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
    if isinstance(result.output, bytes):
        return result.output.decode(errors="replace")
    return str(result.output)


async def reload_nginx() -> dict:
    container = proxy_svc.find_nginx_container()
    if container is None:
        return {"ok": False, "error": "Website hosting is not running. Open Websites and start it first."}
    test = container.exec_run(["nginx", "-t"], demux=True)
    test_log = _exec_output(test).strip()
    if test.exit_code != 0:
        return {
            "ok": False,
            "exit_code": test.exit_code,
            "log": test_log or "Website config check failed.",
            "error": test_log or "Website config check failed.",
        }
    result = container.exec_run(["nginx", "-s", "reload"], demux=True)
    out = _exec_output(result)
    log = "\n".join(part for part in (test_log, out.strip()) if part)
    return {"ok": result.exit_code == 0, "exit_code": result.exit_code, "log": log}
