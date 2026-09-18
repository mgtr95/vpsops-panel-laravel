from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import smtplib
import socket
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from html import escape
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.utils import read_json, write_json

PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "gmail",
        "title": "Gmail",
        "blurb": "Personal Google mail",
        "host": "smtp.gmail.com",
        "port": 587,
        "encryption": "starttls",
        "username_hint": "Your full Gmail address",
        "password_label": "App password",
        "steps": [
            {
                "text": "Open your Google Account security page and make sure 2-Step Verification is on. Google will not let you create an app password without it.",
                "href": "https://myaccount.google.com/signinoptions/two-step-verification",
                "link": "Turn on 2-Step Verification",
            },
            {
                "text": "Open App passwords. Google may ask you to sign in again.",
                "href": "https://myaccount.google.com/apppasswords",
                "link": "Open App passwords",
            },
            {
                "text": "In App name type “VPS backups”, then click Create.",
            },
            {
                "text": "Google shows a 16-character password (often in groups of 4). Copy it. On the next screen you will paste it — do not use your normal Gmail password.",
            },
        ],
        "note": "A normal Gmail password will not work. You must use the 16-character app password.",
    },
    {
        "id": "outlook",
        "title": "Outlook / Microsoft",
        "blurb": "Outlook.com, Hotmail, or Microsoft 365",
        "host": "smtp.office365.com",
        "port": 587,
        "encryption": "starttls",
        "username_hint": "Your full Outlook or Microsoft email",
        "password_label": "App password",
        "steps": [
            {
                "text": "Open Microsoft account security and turn on two-step verification if it is not already on.",
                "href": "https://account.microsoft.com/security",
                "link": "Microsoft account security",
            },
            {
                "text": "Create an app password. Copy the password Microsoft shows you — not your regular Microsoft password.",
                "href": "https://account.live.com/proofs/AppPassword",
                "link": "Create an app password",
            },
            {
                "text": "If this is a work or school Microsoft 365 mailbox and the test fails, an admin may need to enable SMTP AUTH for your account.",
            },
        ],
        "note": "Use the app password, not the password you type when signing in to Outlook.",
    },
    {
        "id": "yahoo",
        "title": "Yahoo Mail",
        "blurb": "Yahoo.com mail",
        "host": "smtp.mail.yahoo.com",
        "port": 587,
        "encryption": "starttls",
        "username_hint": "Your full Yahoo email",
        "password_label": "App password",
        "steps": [
            {
                "text": "Open Yahoo Account security and turn on two-step verification.",
                "href": "https://login.yahoo.com/account/security",
                "link": "Yahoo Account security",
            },
            {
                "text": "Generate an app password (sometimes listed as “Generate app password” or “App passwords”).",
            },
            {
                "text": "Copy that app password. On the next screen you will paste it — do not use your normal Yahoo password.",
            },
        ],
        "note": "Yahoo blocks normal passwords for this kind of sending. An app password is required.",
    },
    {
        "id": "icloud",
        "title": "iCloud Mail",
        "blurb": "Apple iCloud email",
        "host": "smtp.mail.me.com",
        "port": 587,
        "encryption": "starttls",
        "username_hint": "Your full iCloud email (example@icloud.com)",
        "password_label": "App-specific password",
        "steps": [
            {
                "text": "Sign in to your Apple ID. Two-factor authentication must be on.",
                "href": "https://appleid.apple.com",
                "link": "appleid.apple.com",
            },
            {
                "text": "Under Sign-In and Security, open App-Specific Passwords and generate one named “VPS backups”.",
            },
            {
                "text": "Copy the password Apple shows. Use your full iCloud address as the email — not an alias that is not the Apple ID.",
            },
        ],
        "note": "Use an app-specific password, not your Apple ID password.",
    },
    {
        "id": "custom",
        "title": "Other",
        "blurb": "A custom mail server (SMTP)",
        "host": "",
        "port": 587,
        "encryption": "starttls",
        "username_hint": "Usually your full email address",
        "password_label": "Password",
        "steps": [
            {
                "text": "Your mail host should give you an SMTP server name (for example mail.example.com).",
            },
            {
                "text": "Port 587 with STARTTLS is the usual choice. Port 465 with SSL is the other common option. Port 25 is often blocked on a VPS.",
            },
            {
                "text": "Username is usually the full email address. If signing in with your normal password fails, look for an “app password” in the mail settings.",
            },
        ],
        "note": "If the test times out, the VPS may be blocking that port — try 587 or 465.",
    },
]

PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def list_providers() -> list[dict[str, Any]]:
    return PROVIDERS


def _fernet() -> Fernet:
    secret = get_settings().session_secret.encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as e:
        raise ValueError("Could not read the stored mail password. Try saving the account again.") from e


def _accounts_path():
    return get_settings().mail_accounts_path


def list_accounts_raw() -> list[dict]:
    data = read_json(_accounts_path(), [])
    return data if isinstance(data, list) else []


def save_accounts(accounts: list[dict]) -> None:
    write_json(_accounts_path(), accounts)


def public_account(account: dict) -> dict:
    last_test = account.get("last_test") or {}
    return {
        "id": account.get("id"),
        "name": account.get("name"),
        "provider": account.get("provider"),
        "host": account.get("host"),
        "port": account.get("port"),
        "encryption": account.get("encryption"),
        "username": account.get("username"),
        "from_email": account.get("from_email"),
        "from_name": account.get("from_name") or "VPS Backups",
        "has_password": bool(account.get("password_encrypted")),
        "last_test": {
            "ok": last_test.get("ok"),
            "at": last_test.get("at"),
            "error": last_test.get("error"),
        }
        if last_test
        else None,
    }


def list_accounts() -> list[dict]:
    return [public_account(a) for a in list_accounts_raw()]


def get_account(account_id: str) -> dict | None:
    for account in list_accounts_raw():
        if account.get("id") == account_id:
            return account
    return None


def _valid_email(value: str) -> bool:
    addr = parseaddr(value or "")[1].strip()
    return bool(addr) and bool(_EMAIL_RE.match(addr))


def _provider_defaults(provider_id: str) -> dict[str, Any]:
    preset = PROVIDER_BY_ID.get(provider_id)
    if not preset:
        raise ValueError("Choose Gmail, Outlook, Yahoo, iCloud, or Other.")
    return preset


def normalize_account_input(body: dict, existing: dict | None = None) -> dict:
    provider_id = (body.get("provider") or (existing or {}).get("provider") or "").strip().lower()
    preset = _provider_defaults(provider_id)
    name = (body.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        raise ValueError("Give this mailbox a short name you will recognize later.")

    from_email = (body.get("from_email") or (existing or {}).get("from_email") or "").strip()
    if not _valid_email(from_email):
        raise ValueError("Enter a valid email address to send from.")

    from_name = (body.get("from_name") or (existing or {}).get("from_name") or "VPS Backups").strip() or "VPS Backups"
    username = (body.get("username") or from_email).strip() or from_email

    if provider_id == "custom":
        host = (body.get("host") or (existing or {}).get("host") or "").strip()
        if not host:
            raise ValueError("Enter the SMTP server name (for example mail.example.com).")
        try:
            port = int(body.get("port") if body.get("port") not in (None, "") else (existing or {}).get("port") or 587)
        except (TypeError, ValueError) as e:
            raise ValueError("Port must be a number, usually 587 or 465.") from e
        encryption = (body.get("encryption") or (existing or {}).get("encryption") or "starttls").strip().lower()
        if encryption not in ("starttls", "ssl", "none"):
            raise ValueError("Encryption must be STARTTLS, SSL, or none.")
    else:
        host = preset["host"]
        port = int(preset["port"])
        encryption = preset["encryption"]

    if port < 1 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")

    account = {
        **(existing or {}),
        "name": name,
        "provider": provider_id,
        "host": host,
        "port": port,
        "encryption": encryption,
        "username": username,
        "from_email": from_email,
        "from_name": from_name,
    }
    password = body.get("password")
    if isinstance(password, str) and password.strip():
        account["password_encrypted"] = encrypt_password(password.strip())
    elif not account.get("password_encrypted"):
        raise ValueError("Enter the app password (or mail password) so we can send email.")
    return account


def upsert_account(body: dict) -> dict:
    accounts = list_accounts_raw()
    existing = None
    account_id = (body.get("id") or "").strip()
    if account_id:
        for acc in accounts:
            if acc.get("id") == account_id:
                existing = acc
                break
        if existing is None:
            raise ValueError("Mail account not found.")
    account = normalize_account_input(body, existing)
    if not account.get("id"):
        account["id"] = str(uuid.uuid4())[:8]
        account["created_at"] = datetime.now(timezone.utc).isoformat()
    account["updated_at"] = datetime.now(timezone.utc).isoformat()

    replaced = False
    for i, acc in enumerate(accounts):
        if acc.get("id") == account["id"]:
            accounts[i] = account
            replaced = True
            break
    if not replaced:
        accounts.append(account)
    if body.get("tested"):
        account["last_test"] = {
            "ok": True,
            "at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        for i, acc in enumerate(accounts):
            if acc.get("id") == account["id"]:
                accounts[i] = account
                break
    save_accounts(accounts)
    return public_account(account)


def delete_account(account_id: str) -> bool:
    accounts = list_accounts_raw()
    new_accounts = [a for a in accounts if a.get("id") != account_id]
    if len(new_accounts) == len(accounts):
        return False
    save_accounts(new_accounts)
    from app.services import backup_svc

    backup_svc.clear_notify_for_account(account_id)
    return True


def friendly_smtp_error(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "The mail server rejected the sign-in. For Gmail, Outlook, Yahoo, and iCloud "
            "you must use an app password, not your normal login password."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address. Check “Send notifications to”."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The mail server refused the From address. Check the email you are sending from."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return (
            "Timed out reaching the mail server. Port 25 is often blocked on a VPS — "
            "use port 587 (STARTTLS) or 465 (SSL)."
        )
    if isinstance(exc, ConnectionRefusedError):
        return "The mail server refused the connection. Check the server name and port."
    if isinstance(exc, socket.gaierror):
        return "Could not find that mail server. Check the SMTP host name."
    if isinstance(exc, ssl.SSLError):
        return "Could not start a secure connection. Try STARTTLS on port 587, or SSL on port 465."
    if isinstance(exc, smtplib.SMTPException):
        msg = str(exc).strip() or exc.__class__.__name__
        return f"The mail server returned an error: {msg}"
    msg = str(exc).strip() or exc.__class__.__name__
    return f"Could not send email: {msg}"


def _build_message(
    account: dict,
    to_email: str,
    subject: str,
    text: str,
    html: str,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((account.get("from_name") or "VPS Backups", account["from_email"]))
    msg["To"] = to_email
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def _smtp_send(account: dict, password: str, to_email: str, subject: str, text: str, html: str) -> None:
    host = account["host"]
    port = int(account["port"])
    encryption = (account.get("encryption") or "starttls").lower()
    username = account.get("username") or account["from_email"]
    message = _build_message(account, to_email, subject, text, html)
    timeout = 30

    if encryption == "ssl":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if encryption == "starttls":
            context = ssl.create_default_context()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)


def send_message_sync(account: dict, subject: str, text: str, html: str, to_email: str | None = None) -> None:
    dest = (to_email or "").strip()
    if not dest:
        raise ValueError("No recipient address is set.")
    password = decrypt_password(account.get("password_encrypted") or "")
    try:
        _smtp_send(account, password, dest, subject, text, html)
    except Exception as e:
        raise ValueError(friendly_smtp_error(e)) from e


async def send_message(account: dict, subject: str, text: str, html: str, to_email: str | None = None) -> None:
    await asyncio.to_thread(send_message_sync, account, subject, text, html, to_email)


def _test_copy(account: dict) -> tuple[str, str, str]:
    subject = "VPS Panel — test email"
    text = (
        f"This is a test from your VPS panel.\n\n"
        f"Mailbox: {account.get('name')}\n"
        f"Sending as: {account.get('from_email')}\n"
        f"If you can read this, backup notifications can use this account.\n"
    )
    html = (
        "<p>This is a test from your VPS panel.</p>"
        f"<p>Mailbox: <strong>{escape(account.get('name') or '')}</strong><br>"
        f"Sending as: {escape(account.get('from_email') or '')}</p>"
        "<p>If you can read this, backup notifications can use this account.</p>"
    )
    return subject, text, html


async def send_test_for_body(body: dict) -> dict:
    """Test unsaved (or updated) settings without requiring a stored id."""
    existing = get_account(body["id"]) if body.get("id") else None
    account = normalize_account_input(body, existing)
    subject, text, html = _test_copy(account)
    await send_message(account, subject, text, html, to_email=account["from_email"])
    return {"ok": True, "to": account["from_email"]}


async def send_test(account_id: str) -> dict:
    account = get_account(account_id)
    if not account:
        raise ValueError("Mail account not found.")
    subject, text, html = _test_copy(account)
    try:
        await send_message(account, subject, text, html, to_email=account["from_email"])
    except Exception:
        _record_test(account_id, ok=False, error=True)
        raise
    _record_test(account_id, ok=True)
    return {"ok": True, "to": account["from_email"]}


def _record_test(account_id: str, ok: bool, error: bool = False) -> None:
    accounts = list_accounts_raw()
    for acc in accounts:
        if acc.get("id") == account_id:
            acc["last_test"] = {
                "ok": ok,
                "at": datetime.now(timezone.utc).isoformat(),
                "error": "failed" if error else None,
            }
            break
    save_accounts(accounts)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_when(value: str | None, zone) -> str:
    dt = _parse_iso(value)
    if not dt:
        return value or "—"
    return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _log_excerpt(log: str, limit: int = 1200) -> str:
    text = (log or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def _is_file_sync_job(job: dict) -> bool:
    stype = (job.get("source") or {}).get("type")
    return stype in ("app_files", "path")


def _format_byte_size(num: int) -> str:
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{int(num)} B"


def _format_sync_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} m {secs} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m"


def _sync_stats_text_block(stats: dict) -> str:
    transfers = int(stats.get("transfers") or 0)
    checks = int(stats.get("checks") or 0)
    deletes = int(stats.get("deletes") or 0)
    bytes_ = int(stats.get("bytes") or 0)
    elapsed = float(stats.get("elapsedTime") or 0.0)
    return (
        f"Uploaded or updated: {transfers} files ({_format_byte_size(bytes_)})\n"
        f"Unchanged: {checks} files\n"
        f"Removed on destination: {deletes} files\n"
        f"Sync duration: {_format_sync_duration(elapsed)}"
    )


def _sync_stats_html_rows(stats: dict) -> str:
    transfers = int(stats.get("transfers") or 0)
    checks = int(stats.get("checks") or 0)
    deletes = int(stats.get("deletes") or 0)
    bytes_ = int(stats.get("bytes") or 0)
    elapsed = float(stats.get("elapsedTime") or 0.0)
    return (
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Uploaded or updated</td>"
        f"<td>{transfers} files ({escape(_format_byte_size(bytes_))})</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Unchanged</td><td>{checks} files</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Removed on destination</td>"
        f"<td>{deletes} files</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Sync duration</td>"
        f"<td>{escape(_format_sync_duration(elapsed))}</td></tr>"
    )


def _apply_email_placeholders(template: str, name: str, ok: bool) -> str:
    status = "OK" if ok else "FAILED"
    return template.replace("{name}", name).replace("{status}", status)


def _notify_copy(notify: dict, ok: bool) -> tuple[str, str, str]:
    suffix = "success" if ok else "failure"
    subject = (notify.get(f"subject_{suffix}") or notify.get("subject") or "").strip()
    title = (notify.get(f"title_{suffix}") or notify.get("title") or "").strip()
    message = (notify.get(f"message_{suffix}") or notify.get("message") or "").strip()
    return subject, title, message


def build_backup_email(job: dict, entry: dict, *, include_details: bool = True) -> tuple[str, str, str]:
    from app.services.backup_svc import backup_zone

    zone = backup_zone(job)
    name = job.get("name") or "Backup"
    status = entry.get("status") or "unknown"
    ok = status == "success"
    notify = job.get("notify") or {}
    started = _format_when(entry.get("started_at"), zone)
    finished = _format_when(entry.get("finished_at"), zone)
    dest = job.get("destination") or "—"
    log = _log_excerpt(entry.get("log") or "")
    status_label = "succeeded" if ok else "failed"
    custom_subject, custom_title, intro = _notify_copy(notify, ok)
    subject = (
        _apply_email_placeholders(custom_subject, name, ok)
        if custom_subject
        else f"[Backup {'OK' if ok else 'FAILED'}] {name}"
    )
    title = _apply_email_placeholders(custom_title, name, ok) if custom_title else f"Backup {status_label}"

    text_parts = [title]
    if intro:
        text_parts.append(intro)
    sync_stats = entry.get("sync_stats") if _is_file_sync_job(job) else None
    if include_details:
        detail = (
            f"Backup: {name}\n"
            f"Status: {status_label}\n"
            f"Started: {started}\n"
            f"Finished: {finished}\n"
            f"Saved to: {dest}\n"
        )
        if sync_stats:
            detail += f"\n{_sync_stats_text_block(sync_stats)}\n"
        detail += f"\nLog:\n{log}\n"
        text_parts.append(detail)
    text = "\n\n".join(text_parts)

    html_intro = ""
    if intro:
        html_intro = (
            f"<p style='font-family:sans-serif;margin:0 0 16px;white-space:pre-wrap'>{escape(intro)}</p>"
        )
    html = (
        f"<h2 style='margin:0 0 12px;font-family:sans-serif'>{escape(title)}</h2>"
        f"{html_intro}"
    )
    if include_details:
        color = "#3d9a6a" if ok else "#d45d5d"
        html_log = escape(log).replace("\n", "<br>")
        html += (
            f"<p style='font-family:sans-serif;margin:0 0 8px'><strong>{escape(name)}</strong></p>"
            f"<p style='font-family:sans-serif;margin:0 0 12px'>"
            f"<span style='display:inline-block;padding:2px 8px;border-radius:6px;"
            f"background:{color};color:#fff;font-size:13px'>{escape(status)}</span></p>"
            "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse'>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Started</td><td>{escape(started)}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Finished</td><td>{escape(finished)}</td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Saved to</td><td>{escape(str(dest))}</td></tr>"
        )
        if sync_stats:
            html += _sync_stats_html_rows(sync_stats)
        html += (
            "</table>"
            f"<p style='font-family:monospace;font-size:12px;background:#f4f4f4;padding:12px;"
            f"white-space:normal;margin-top:16px'>{html_log}</p>"
        )
    return subject, text, html


def resolve_notify_recipient_targets(notify: dict) -> list[dict[str, Any]]:
    from app.services import recipients_svc

    targets: list[dict[str, Any]] = []
    ids = notify.get("recipient_ids") or notify.get("contact_ids") or []
    for rid in ids:
        recipient = recipients_svc.get_recipient(str(rid))
        if recipient and recipient.get("email"):
            include_details = recipient.get("include_details", True)
            if not isinstance(include_details, bool):
                include_details = True
            targets.append(
                {
                    "email": str(recipient["email"]).strip(),
                    "include_details": include_details,
                }
            )
    for raw in notify.get("to_emails") or []:
        if isinstance(raw, str):
            addr = raw.strip()
            if addr and _valid_email(addr):
                targets.append({"email": addr, "include_details": True})
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for target in targets:
        key = target["email"].lower()
        if key not in seen:
            seen.add(key)
            out.append(target)
    return out


def resolve_notify_recipients(notify: dict) -> list[str]:
    return [target["email"] for target in resolve_notify_recipient_targets(notify)]


async def send_message_to_recipients(
    account: dict,
    subject: str,
    text: str,
    html: str,
    recipients: list[str],
) -> None:
    if not recipients:
        raise ValueError("No recipient address is set.")
    for dest in recipients:
        await send_message(account, subject, text, html, to_email=dest)


async def send_backup_email_to_recipients(
    account: dict,
    job: dict,
    entry: dict,
    targets: list[dict[str, Any]],
) -> None:
    if not targets:
        raise ValueError("No recipient address is set.")
    full = build_backup_email(job, entry, include_details=True)
    simple = build_backup_email(job, entry, include_details=False)
    for target in targets:
        subject, text, html = full if target.get("include_details", True) else simple
        await send_message(account, subject, text, html, to_email=target["email"])


def should_notify(job: dict, status: str) -> bool:
    notify = job.get("notify") or {}
    if (notify.get("group_id") or "").strip():
        return False
    if not notify.get("enabled"):
        return False
    if not (notify.get("account_id") or "").strip():
        return False
    when = (notify.get("on") or "always").strip().lower()
    if when == "always":
        return True
    if when in ("failure", "failed"):
        return status == "failed"
    if when == "success":
        return status == "success"
    return False


async def notify_backup(job: dict, entry: dict) -> None:
    if not should_notify(job, entry.get("status") or ""):
        return
    notify = job.get("notify") or {}
    account_id = notify.get("account_id")
    account = get_account(account_id)
    if not account:
        raise ValueError("The selected mail account is no longer set up. Open Mail and add it again.")
    targets = resolve_notify_recipient_targets(notify)
    if not targets:
        raise ValueError("Choose at least one recipient on the Mail page, or add extra emails for this backup.")
    await send_backup_email_to_recipients(account, job, entry, targets)


def should_notify_group(notify: dict, statuses: list[str | None]) -> bool:
    if not notify.get("enabled"):
        return False
    if not (notify.get("account_id") or "").strip():
        return False
    when = (notify.get("on") or "always").strip().lower()
    if when == "always":
        return True
    if when in ("failure", "failed"):
        return any(s == "failed" for s in statuses)
    if when == "success":
        return bool(statuses) and all(s == "success" for s in statuses)
    return False


def build_group_backup_email(
    group: dict,
    entries: list[dict],
    jobs: dict[str, dict],
    missing_names: list[str] | None = None,
    *,
    include_details: bool = True,
) -> tuple[str, str, str]:
    from app.services.backup_svc import backup_zone

    notify = group.get("notify") or {}
    group_name = group.get("name") or "Backup group"
    statuses = [e.get("status") for e in entries]
    any_failed = any(s == "failed" for s in statuses)
    ok = not any_failed

    custom_subject, custom_title, intro = _notify_copy(notify, ok)
    subject = (
        _apply_email_placeholders(custom_subject, group_name, ok)
        if custom_subject
        else f"[Backup report] {group_name}"
    )
    title = (
        _apply_email_placeholders(custom_title, group_name, ok)
        if custom_title
        else f"Backup report — {group_name}"
    )

    text_lines = [title]
    if intro:
        text_lines.append(intro)
    html_rows = []
    if include_details:
        text_lines.append(f"Group: {group_name}\n")
        text_lines.append("Backups:")
        for entry in entries:
            job_id = entry.get("job_id")
            job = jobs.get(job_id) or {}
            name = entry.get("job_name") or job.get("name") or job_id or "Backup"
            status = entry.get("status") or "unknown"
            zone = backup_zone(job)
            started = _format_when(entry.get("started_at"), zone)
            finished = _format_when(entry.get("finished_at"), zone)
            dest = job.get("destination") or "—"
            log = _log_excerpt(entry.get("log") or "", limit=400)
            status_label = "succeeded" if status == "success" else "failed"
            sync_stats = entry.get("sync_stats") if _is_file_sync_job(job) else None
            block = (
                f"\n— {name}\n"
                f"  Status: {status_label}\n"
                f"  Started: {started}\n"
                f"  Finished: {finished}\n"
                f"  Saved to: {dest}\n"
            )
            if sync_stats:
                indented = _sync_stats_text_block(sync_stats).replace("\n", "\n  ")
                block += f"  {indented}\n"
            block += f"  Log: {log}\n"
            text_lines.append(block)
            color = "#3d9a6a" if status == "success" else "#d45d5d"
            html_log = escape(log).replace("\n", "<br>")
            sync_html = ""
            if sync_stats:
                sync_html = (
                    f"<table style='font-family:sans-serif;font-size:12px;margin:4px 0 8px'>"
                    f"{_sync_stats_html_rows(sync_stats)}"
                    "</table>"
                )
            html_rows.append(
                f"<tr><td style='padding:8px 12px 8px 0;vertical-align:top'><strong>{escape(name)}</strong></td>"
                f"<td style='padding:8px 12px'>"
                f"<span style='display:inline-block;padding:2px 8px;border-radius:6px;"
                f"background:{color};color:#fff;font-size:13px'>{escape(status_label)}</span></td>"
                f"<td style='padding:8px 12px;color:#555;font-size:13px'>{escape(started)} → {escape(finished)}</td>"
                f"<td style='padding:8px 12px;color:#555;font-size:13px'>{escape(str(dest))}</td></tr>"
                f"<tr><td colspan='4' style='padding:0 12px 8px 0'>{sync_html}"
                f"<pre style='font-family:monospace;font-size:11px;background:#f4f4f4;padding:8px;"
                f"white-space:pre-wrap;margin:0'>{html_log}</pre></td></tr>"
            )

        if missing_names:
            missing_text = ", ".join(missing_names)
            text_lines.append(f"\nNot yet run (timeout): {missing_text}\n")
    text = "\n\n".join(text_lines)

    html_intro = ""
    if intro:
        html_intro = (
            f"<p style='font-family:sans-serif;margin:0 0 16px;white-space:pre-wrap'>{escape(intro)}</p>"
        )
    missing_html = ""
    if include_details and missing_names:
        missing_html = (
            f"<p style='font-family:sans-serif;color:#a66;font-size:14px'>"
            f"These backups had not finished before the timeout: "
            f"<strong>{escape(', '.join(missing_names))}</strong></p>"
        )
    html = (
        f"<h2 style='margin:0 0 12px;font-family:sans-serif'>{escape(title)}</h2>"
        f"{html_intro}"
    )
    if include_details:
        html += (
            f"<p style='font-family:sans-serif;margin:0 0 12px'>Group: <strong>{escape(group_name)}</strong></p>"
            "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse;width:100%'>"
            "<tr style='background:#eee'>"
            "<th style='text-align:left;padding:8px 12px'>Backup</th>"
            "<th style='text-align:left;padding:8px 12px'>Status</th>"
            "<th style='text-align:left;padding:8px 12px'>When</th>"
            "<th style='text-align:left;padding:8px 12px'>Destination</th></tr>"
            f"{''.join(html_rows)}"
            "</table>"
            f"{missing_html}"
        )
    return subject, text, html


async def send_group_backup_email_to_recipients(
    account: dict,
    group: dict,
    entries: list[dict],
    jobs: dict[str, dict],
    targets: list[dict[str, Any]],
    missing_names: list[str] | None = None,
) -> None:
    if not targets:
        raise ValueError("No recipient address is set.")
    full = build_group_backup_email(group, entries, jobs, missing_names, include_details=True)
    simple = build_group_backup_email(group, entries, jobs, missing_names, include_details=False)
    for target in targets:
        subject, text, html = full if target.get("include_details", True) else simple
        await send_message(account, subject, text, html, to_email=target["email"])


async def notify_backup_group(
    group: dict,
    entries: list[dict],
    jobs: dict[str, dict],
    missing_names: list[str] | None = None,
) -> None:
    notify = group.get("notify") or {}
    account = get_account(notify.get("account_id"))
    if not account:
        raise ValueError("The selected mail account is no longer set up. Open Mail and add it again.")
    targets = resolve_notify_recipient_targets(notify)
    if not targets:
        raise ValueError("Choose at least one recipient on the Mail page, or add extra emails for this group.")
    await send_group_backup_email_to_recipients(
        account, group, entries, jobs, targets, missing_names
    )


CONTAINER_EVENT_LABELS: dict[str, str] = {
    "crash": "Crashed",
    "restart_loop": "Restart loop",
    "unhealthy": "Unhealthy",
    "oom": "Out of memory",
    "restart_count": "Restarted",
    "log_error": "Log error",
}


def build_container_alert_email(
    container: dict,
    event_type: str,
    details: dict,
    *,
    test: bool = False,
) -> tuple[str, str, str]:
    name = container.get("name") or "container"
    image = container.get("image") or "—"
    status = container.get("status") or "—"
    health = container.get("health") or "—"
    compose = container.get("compose_project") or ""
    issue = details.get("issue") or container.get("issue") or {}
    title = details.get("title") or CONTAINER_EVENT_LABELS.get(event_type, event_type)
    event_label = CONTAINER_EVENT_LABELS.get(event_type, event_type)

    prefix = "[Container alert test]" if test else "[Container alert]"
    subject = f"{prefix} {name} — {title}"

    summary = issue.get("summary") or ""
    suggestion = issue.get("suggestion") or ""
    log_lines = details.get("log_lines") or []

    text_parts = [
        f"Container: {name}",
        f"Event: {event_label}",
        f"Image: {image}",
        f"Status: {status}",
        f"Health: {health}",
    ]
    if compose:
        text_parts.append(f"Compose project: {compose}")
    if summary:
        text_parts.append(f"\n{summary}")
    if suggestion:
        text_parts.append(f"\nSuggestion: {suggestion}")
    if log_lines:
        text_parts.append("\nRecent log lines:")
        text_parts.extend(log_lines)
    if test:
        text_parts.insert(0, "This is a test alert from your VPS dashboard.\n")
    text = "\n".join(text_parts)

    html_summary = escape(summary) if summary else ""
    html_suggestion = escape(suggestion) if suggestion else ""
    html_logs = ""
    if log_lines:
        joined = escape("\n".join(log_lines))
        html_logs = (
            "<p style='font-family:sans-serif;margin:16px 0 4px'><strong>Recent log lines</strong></p>"
            "<pre style='font-family:monospace;font-size:12px;background:#f4f4f4;padding:12px;"
            "white-space:pre-wrap;margin:0'>"
            f"{joined}</pre>"
        )
    test_banner = (
        "<p style='font-family:sans-serif;color:#555;margin:0 0 12px'>"
        "This is a test alert from your VPS dashboard.</p>"
        if test
        else ""
    )
    compose_row = (
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Compose</td>"
        f"<td>{escape(compose)}</td></tr>"
        if compose
        else ""
    )
    html = (
        f"{test_banner}"
        f"<p style='font-family:sans-serif;margin:0 0 8px'><strong>{escape(name)}</strong>"
        f" — {escape(event_label)}</p>"
        "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Image</td><td>{escape(image)}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Status</td><td>{escape(status)}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Health</td><td>{escape(str(health))}</td></tr>"
        f"{compose_row}"
        "</table>"
        f"{f'<p style=\"font-family:sans-serif;margin:12px 0 0\">{html_summary}</p>' if html_summary else ''}"
        f"{f'<p style=\"font-family:sans-serif;margin:8px 0 0;color:#555\">{html_suggestion}</p>' if html_suggestion else ''}"
        f"{html_logs}"
    )
    return subject, text, html


async def notify_container_event(
    config: dict,
    container: dict,
    event_type: str,
    details: dict,
    *,
    test: bool = False,
) -> None:
    notify = config.get("notify") or {}
    account = get_account(notify.get("account_id"))
    if not account:
        raise ValueError("The selected mail account is no longer set up. Open Mail and add it again.")
    subject, text, html = build_container_alert_email(
        container, event_type, details, test=test
    )
    recipients = resolve_notify_recipients(notify)
    if not recipients:
        raise ValueError("Choose at least one recipient on the Mail page, or add extra emails.")
    await send_message_to_recipients(account, subject, text, html, recipients)


CERT_EVENT_LABELS: dict[str, str] = {
    "expiring_soon": "Expiring soon",
    "expired": "Expired",
    "renewal_success": "Renewal succeeded",
    "renewal_failed": "Renewal failed",
    "served_mismatch": "Certificate mismatch",
}


def build_cert_alert_email(
    cert: dict,
    event_type: str,
    details: dict,
    *,
    test: bool = False,
) -> tuple[str, str, str]:
    domain = cert.get("domain") or "certificate"
    days_left = cert.get("days_left")
    not_after = cert.get("not_after") or details.get("not_after") or "—"
    issuer = cert.get("issuer") or details.get("issuer") or "—"
    title = details.get("title") or CERT_EVENT_LABELS.get(event_type, event_type)
    event_label = CERT_EVENT_LABELS.get(event_type, event_type)
    summary = details.get("summary") or ""

    prefix = "[Certificate alert test]" if test else "[Certificate alert]"
    subject = f"{prefix} {domain} — {title}"

    text_parts = [
        f"Domain: {domain}",
        f"Event: {event_label}",
    ]
    if days_left is not None:
        text_parts.append(f"Days left: {days_left}")
    if not_after != "—":
        text_parts.append(f"Valid until: {not_after}")
    if issuer != "—":
        text_parts.append(f"Issuer: {issuer}")
    if summary:
        text_parts.append(f"\n{summary}")
    log_excerpt = details.get("log_excerpt")
    if log_excerpt:
        text_parts.append(f"\nLog excerpt:\n{log_excerpt}")
    if test:
        text_parts.insert(0, "This is a test alert from your VPS dashboard.\n")
    text = "\n".join(text_parts)

    days_row = (
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Days left</td>"
        f"<td>{escape(str(days_left))}</td></tr>"
        if days_left is not None
        else ""
    )
    html_log = ""
    if log_excerpt:
        html_log = (
            "<p style='font-family:sans-serif;margin:16px 0 4px'><strong>Log excerpt</strong></p>"
            "<pre style='font-family:monospace;font-size:12px;background:#f4f4f4;padding:12px;"
            "white-space:pre-wrap;margin:0'>"
            f"{escape(log_excerpt)}</pre>"
        )
    test_banner = (
        "<p style='font-family:sans-serif;color:#555;margin:0 0 12px'>"
        "This is a test alert from your VPS dashboard.</p>"
        if test
        else ""
    )
    html = (
        f"{test_banner}"
        f"<p style='font-family:sans-serif;margin:0 0 8px'><strong>{escape(domain)}</strong>"
        f" — {escape(event_label)}</p>"
        "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse'>"
        f"{days_row}"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Valid until</td>"
        f"<td>{escape(str(not_after))}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>Issuer</td>"
        f"<td>{escape(str(issuer))}</td></tr>"
        "</table>"
        f"{f'<p style=\"font-family:sans-serif;margin:12px 0 0\">{escape(summary)}</p>' if summary else ''}"
        f"{html_log}"
    )
    return subject, text, html


async def notify_cert_event(
    config: dict,
    cert: dict,
    event_type: str,
    details: dict,
    *,
    test: bool = False,
) -> None:
    notify = config.get("notify") or {}
    account = get_account(notify.get("account_id"))
    if not account:
        raise ValueError("The selected mail account is no longer set up. Open Mail and add it again.")
    subject, text, html = build_cert_alert_email(cert, event_type, details, test=test)
    recipients = resolve_notify_recipients(notify)
    if not recipients:
        raise ValueError("Choose at least one recipient on the Mail page, or add extra emails.")
    await send_message_to_recipients(account, subject, text, html, recipients)
