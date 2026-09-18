from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.services import backup_svc, certs_svc, mail_svc
from app.utils import read_json, write_json

logger = logging.getLogger(__name__)

JOB_ID = "cert-alerts"
POLL_HOURS = 6
DEFAULT_COOLDOWN_MINUTES = 1440
DEFAULT_THRESHOLDS = [30, 14, 7, 1]

DEFAULT_EVENTS = {
    "expiring_soon": True,
    "expired": True,
    "renewal_success": True,
    "renewal_failed": True,
    "served_mismatch": False,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "notify": {
        "account_id": "",
        "recipient_ids": [],
        "to_emails": [],
    },
    "events": dict(DEFAULT_EVENTS),
    "thresholds_days": list(DEFAULT_THRESHOLDS),
    "cooldown_minutes": DEFAULT_COOLDOWN_MINUTES,
}


def _config_path():
    return get_settings().cert_alerts_path


def _state_path():
    return get_settings().cert_alert_state_path


def _normalize_notify(notify: dict | None, existing: dict | None = None) -> dict:
    notify = notify or {}
    existing = existing or {}
    account_id = (
        notify.get("account_id")
        if "account_id" in notify
        else existing.get("account_id") or ""
    ).strip()
    recipient_ids = notify.get("recipient_ids")
    if recipient_ids is None:
        recipient_ids = existing.get("recipient_ids") or []
    to_emails = notify.get("to_emails")
    if to_emails is None:
        to_emails = existing.get("to_emails") or []
    return {
        "account_id": account_id,
        "recipient_ids": [str(r).strip() for r in recipient_ids if str(r).strip()],
        "to_emails": [str(e).strip() for e in to_emails if str(e).strip()],
    }


def _normalize_thresholds(raw: list | None, existing: list | None = None) -> list[int]:
    source = raw if raw is not None else (existing or DEFAULT_THRESHOLDS)
    out: list[int] = []
    for item in source:
        try:
            days = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= days <= 365 and days not in out:
            out.append(days)
    return sorted(out, reverse=True) or list(DEFAULT_THRESHOLDS)


def _normalize_events(events: dict | None, existing: dict | None = None) -> dict:
    events = events or {}
    existing = existing or {}
    out = {}
    for key, default in DEFAULT_EVENTS.items():
        if key in events:
            out[key] = bool(events[key])
        elif key in existing:
            out[key] = bool(existing[key])
        else:
            out[key] = default
    return out


def _normalize_config(raw: dict | None, existing: dict | None = None) -> dict:
    raw = raw or {}
    base = existing if existing is not None else DEFAULT_CONFIG
    notify = _normalize_notify(raw.get("notify"), base.get("notify"))
    events = _normalize_events(raw.get("events"), base.get("events"))
    thresholds = _normalize_thresholds(raw.get("thresholds_days"), base.get("thresholds_days"))
    cooldown = raw.get("cooldown_minutes", base.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
    try:
        cooldown = max(60, min(10080, int(cooldown)))
    except (TypeError, ValueError):
        cooldown = DEFAULT_COOLDOWN_MINUTES
    enabled = raw.get("enabled") if "enabled" in raw else base.get("enabled", False)
    return {
        "enabled": bool(enabled),
        "notify": notify,
        "events": events,
        "thresholds_days": thresholds,
        "cooldown_minutes": cooldown,
    }


def get_config() -> dict:
    data = read_json(_config_path(), None)
    if not isinstance(data, dict):
        return _normalize_config({})
    return _normalize_config(data)


def save_config(raw: dict) -> dict:
    current = read_json(_config_path(), None)
    existing = _normalize_config(current) if isinstance(current, dict) else None
    config = _normalize_config(raw, existing)
    write_json(_config_path(), config)
    reschedule_monitor()
    return config


def load_state() -> dict:
    data = read_json(_state_path(), {})
    if not isinstance(data, dict):
        return {"certs": {}, "last_check": None}
    certs = data.get("certs")
    if not isinstance(certs, dict):
        certs = {}
    return {"certs": certs, "last_check": data.get("last_check")}


def save_state(state: dict) -> None:
    write_json(_state_path(), state)


def _can_notify(config: dict) -> bool:
    if not config.get("enabled"):
        return False
    notify = config.get("notify") or {}
    if not (notify.get("account_id") or "").strip():
        return False
    return bool(mail_svc.resolve_notify_recipients(notify))


def _cooldown_active(cooldowns: dict, event: str, now: datetime, minutes: int) -> bool:
    raw = cooldowns.get(event)
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now < until


def _set_cooldown(cooldowns: dict, event: str, now: datetime, minutes: int) -> None:
    cooldowns[event] = (now + timedelta(minutes=minutes)).isoformat()


def _cert_entry(state: dict, domain: str) -> dict:
    return dict(state.get("certs", {}).get(domain) or {})


def _detect_cert_events(
    cert: dict,
    served: dict | None,
    prev: dict,
    config: dict,
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    domain = cert.get("domain") or ""
    days_left = int(cert.get("days_left") or 0)
    expired = bool(cert.get("expired"))
    notified = set(prev.get("notified_thresholds") or [])
    thresholds = config.get("thresholds_days") or DEFAULT_THRESHOLDS
    ev = config.get("events") or {}

    details_base = {
        "domain": domain,
        "days_left": days_left,
        "not_after": cert.get("not_after"),
        "issuer": cert.get("issuer"),
        "sans": cert.get("sans") or [],
    }

    if ev.get("expired") and expired and not prev.get("expired_notified"):
        events.append(
            (
                "expired",
                {
                    **details_base,
                    "title": "Certificate expired",
                    "summary": f"The certificate for {domain} has expired.",
                },
            )
        )

    if ev.get("expiring_soon") and not expired:
        if prev.get("last_days_left") is None:
            applicable = [t for t in thresholds if days_left <= t]
            if applicable:
                threshold = min(applicable)
                events.append(
                    (
                        "expiring_soon",
                        {
                            **details_base,
                            "threshold_days": threshold,
                            "title": f"Expires in {days_left} days",
                            "summary": (
                                f"The certificate for {domain} expires in {days_left} days "
                                f"(threshold: {threshold} days)."
                            ),
                        },
                    )
                )
        else:
            for threshold in thresholds:
                if days_left <= threshold and threshold not in notified:
                    events.append(
                        (
                            "expiring_soon",
                            {
                                **details_base,
                                "threshold_days": threshold,
                                "title": f"Expires in {days_left} days",
                                "summary": (
                                    f"The certificate for {domain} expires in {days_left} days "
                                    f"(threshold: {threshold} days)."
                                ),
                            },
                        )
                    )

    if ev.get("served_mismatch") and served is not None:
        match = served.get("match")
        if match is False and prev.get("last_match") is not False:
            events.append(
                (
                    "served_mismatch",
                    {
                        **details_base,
                        "title": "Certificate mismatch",
                        "summary": (
                            f"The certificate served by {domain} does not match the one on disk. "
                            "Reload websites or check nginx config."
                        ),
                        "served_enddate": served.get("served_enddate"),
                    },
                )
            )

    return events


def _update_cert_state(
    cert: dict,
    served: dict | None,
    prev: dict,
    events: list[tuple[str, dict]],
    cooldowns: dict,
    now: datetime,
    cooldown_minutes: int,
    config: dict,
) -> dict:
    days_left = int(cert.get("days_left") or 0)
    expired = bool(cert.get("expired"))
    notified = set(prev.get("notified_thresholds") or [])

    if prev.get("last_days_left") is not None and days_left > int(prev.get("last_days_left") or 0) + 7:
        notified = set()
        prev_expired = False
    else:
        prev_expired = bool(prev.get("expired_notified"))

    for event_type, details in events:
        if event_type == "expiring_soon":
            threshold = details.get("threshold_days")
            if threshold is not None:
                notified.add(int(threshold))
                # Mark higher thresholds as notified too when catching up
                for t in config.get("thresholds_days") or DEFAULT_THRESHOLDS:
                    if t >= int(threshold):
                        notified.add(int(t))
        elif event_type == "expired":
            prev_expired = True
        cd_key = event_type
        if event_type == "expiring_soon":
            cd_key = f"expiring_{details.get('threshold_days')}"
        _set_cooldown(cooldowns, cd_key, now, cooldown_minutes)

    match = served.get("match") if served else prev.get("last_match")

    return {
        "last_days_left": days_left,
        "notified_thresholds": sorted(notified, reverse=True),
        "expired_notified": prev_expired or expired,
        "last_match": match,
        "cooldowns": cooldowns,
    }


async def _send_events(config: dict, events: list[tuple[dict, str, dict]]) -> None:
    for cert, event_type, details in events:
        try:
            await mail_svc.notify_cert_event(config, cert, event_type, details)
        except Exception as exc:
            logger.exception(
                "Failed to send cert alert for %s (%s): %s",
                cert.get("domain"),
                event_type,
                exc,
            )


def check_certificates() -> None:
    config = get_config()
    if not config.get("enabled"):
        return

    state = load_state()
    now = datetime.now(timezone.utc)
    cooldown_minutes = int(config.get("cooldown_minutes") or DEFAULT_COOLDOWN_MINUTES)
    can_send = _can_notify(config)

    try:
        certs = certs_svc.list_certificates()
        served_list = certs_svc.check_served()
    except Exception as exc:
        logger.warning("Cert alert poll failed: %s", exc)
        return

    served_by_domain = {s["domain"]: s for s in served_list}
    known = state.get("certs") or {}
    next_known: dict[str, dict] = {}
    pending: list[tuple[dict, str, dict]] = []

    for cert in certs:
        domain = cert.get("domain") or ""
        if not domain:
            continue

        prev = _cert_entry(state, domain)
        cooldowns = dict(prev.get("cooldowns") or {})
        served = served_by_domain.get(domain)

        detected = _detect_cert_events(cert, served, prev, config)

        filtered: list[tuple[str, dict]] = []
        for event_type, details in detected:
            cd_key = event_type
            if event_type == "expiring_soon":
                cd_key = f"expiring_{details.get('threshold_days')}"
            if _cooldown_active(cooldowns, cd_key, now, cooldown_minutes):
                continue
            filtered.append((event_type, details))

        if filtered and can_send:
            for event_type, details in filtered:
                pending.append((cert, event_type, details))

        next_known[domain] = _update_cert_state(
            cert, served, prev, filtered, cooldowns, now, cooldown_minutes, config
        )

    # Drop removed domains from state
    state["certs"] = next_known
    state["last_check"] = now.isoformat()
    save_state(state)

    if pending:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send_events(config, pending))
        finally:
            loop.close()


async def notify_renewal(result: dict) -> None:
    config = get_config()
    if not config.get("enabled") or not _can_notify(config):
        return

    events = config.get("events") or {}
    ok = bool(result.get("ok"))
    log_text = (result.get("log") or "").lower()
    actually_renewed = "congratulations" in log_text or "renewed" in log_text

    if ok:
        if not events.get("renewal_success") or not actually_renewed:
            return
        event_type = "renewal_success"
        details = {
            "title": "Certificate renewal succeeded",
            "summary": "One or more certificates were renewed successfully.",
            "log_excerpt": (result.get("log") or "")[-800:],
        }
    else:
        if not events.get("renewal_failed"):
            return
        event_type = "renewal_failed"
        details = {
            "title": "Certificate renewal failed",
            "summary": result.get("error") or "Certificate renewal failed. Check the log below.",
            "log_excerpt": (result.get("log") or "")[-800:],
            "exit_code": result.get("exit_code"),
        }

    cert = {"domain": "all certificates", "days_left": None, "not_after": None, "issuer": "Let's Encrypt"}
    try:
        await mail_svc.notify_cert_event(config, cert, event_type, details)
    except Exception as exc:
        logger.exception("Failed to send cert renewal alert: %s", exc)


async def send_test_alert() -> None:
    config = get_config()
    if not _can_notify(config):
        raise ValueError(
            "Enable alerts and choose a mail account with at least one recipient."
        )
    sample = {
        "domain": "example.com",
        "cn": "example.com",
        "sans": ["example.com", "www.example.com"],
        "issuer": "CN=Let's Encrypt Authority X3",
        "not_after": "2026-09-28T12:00:00+00:00",
        "days_left": 14,
        "expired": False,
    }
    details = {
        "title": "Expires in 14 days",
        "summary": "This is a test alert. Your certificate for example.com would expire in 14 days.",
        "threshold_days": 14,
    }
    await mail_svc.notify_cert_event(config, sample, "expiring_soon", details, test=True)


def reschedule_monitor() -> None:
    scheduler = backup_svc.scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job(JOB_ID)
    except Exception:
        pass
    config = get_config()
    if not config.get("enabled"):
        return
    try:
        scheduler.add_job(
            check_certificates,
            IntervalTrigger(hours=POLL_HOURS),
            id=JOB_ID,
            replace_existing=True,
        )
    except Exception as exc:
        logger.warning("Failed to schedule cert alerts: %s", exc)


def start_monitor() -> None:
    reschedule_monitor()
