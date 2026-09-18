from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.services import rclone_oauth


async def rc_call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 120.0,
) -> Any:
    settings = get_settings()
    url = f"{settings.rclone_rc_url.rstrip('/')}/{method.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=params or {})
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            error = data.get("error") or data.get("message") or resp.text
            raise RuntimeError(error)
        return data


async def list_remotes() -> list[str]:
    data = await rc_call("config/listremotes")
    return data.get("remotes") or []


async def get_providers() -> Any:
    return await rc_call("config/providers")


async def dump_config() -> Any:
    return await rc_call("config/dump")


async def create_remote(name: str, type_: str, parameters: dict[str, Any]) -> Any:
    return await rc_call(
        "config/create",
        {"name": name, "type": type_, "parameters": parameters, "opt": {"nonInteractive": True}},
    )


async def update_remote(name: str, parameters: dict[str, Any]) -> Any:
    return await rc_call(
        "config/update",
        {"name": name, "parameters": parameters, "opt": {"nonInteractive": True}},
    )


async def delete_remote(name: str) -> Any:
    return await rc_call("config/delete", {"name": name})


async def get_remote(name: str) -> Any:
    return await rc_call("config/get", {"name": name})


async def operations_list(fs: str, remote: str = "", dirs_only: bool = False) -> Any:
    params: dict[str, Any] = {"fs": fs, "remote": remote}
    if dirs_only:
        params["opt"] = {"dirsOnly": True}
    try:
        return await rc_call("operations/list", params)
    except RuntimeError as exc:
        if await _repair_onedrive_if_missing_drive(fs, exc):
            return await rc_call("operations/list", params)
        raise


async def mkdir(fs: str, remote: str) -> Any:
    return await rc_call("operations/mkdir", {"fs": fs, "remote": remote})


async def delete_file(fs: str, remote: str) -> Any:
    return await rc_call("operations/deletefile", {"fs": fs, "remote": remote})


async def purge(fs: str, remote: str) -> Any:
    return await rc_call("operations/purge", {"fs": fs, "remote": remote})


async def about(fs: str) -> Any:
    try:
        return await rc_call("operations/about", {"fs": fs})
    except RuntimeError as exc:
        if await _repair_onedrive_if_missing_drive(fs, exc):
            return await rc_call("operations/about", {"fs": fs})
        raise


async def _repair_onedrive_if_missing_drive(fs: str, exc: Exception) -> bool:
    if _MISSING_DRIVE_ERR not in str(exc):
        return False
    name = _remote_name_from_fs(fs)
    if not name:
        return False
    return bool(await ensure_onedrive_drive_ids(name))


async def sync_copy_move(
    op: str,
    src_fs: str,
    dst_fs: str,
    *,
    async_job: bool = True,
    create_empty_src_dirs: bool = False,
) -> Any:
    method = {
        "sync": "sync/sync",
        "copy": "sync/copy",
        "move": "sync/move",
        "check": "sync/check",
    }.get(op)
    if not method:
        raise ValueError(f"Unknown op: {op}")
    params: dict[str, Any] = {
        "srcFs": src_fs,
        "dstFs": dst_fs,
        "_async": async_job,
    }
    if op in ("sync", "copy", "move"):
        params["createEmptySrcDirs"] = create_empty_src_dirs
    return await rc_call(method, params, timeout=1800.0 if not async_job else 120.0)


async def job_status(jobid: int) -> Any:
    return await rc_call("job/status", {"jobid": jobid})


async def job_list() -> Any:
    return await rc_call("job/list")


async def core_stats(*, group: str | None = None) -> Any:
    params: dict[str, Any] = {}
    if group:
        params["group"] = group
    return await rc_call("core/stats", params or None)


def normalize_sync_stats(raw: dict[str, Any] | None) -> dict[str, int | float]:
    """Map rclone core/stats fields to a stable shape for history and email."""
    data = raw or {}
    transfers = data.get("totalTransfers")
    if transfers is None:
        transfers = data.get("transfers")
    checks = data.get("totalChecks")
    if checks is None:
        checks = data.get("checks")
    deletes = data.get("deletes")
    if deletes is None:
        deletes = 0
    bytes_ = data.get("totalBytes")
    if bytes_ is None:
        bytes_ = data.get("bytes")
    if bytes_ is None:
        bytes_ = 0
    errors = data.get("errors")
    if errors is None:
        errors = 0
    elapsed = data.get("elapsedTime")
    if elapsed is None:
        elapsed = 0.0
    return {
        "transfers": int(transfers or 0),
        "checks": int(checks or 0),
        "deletes": int(deletes or 0),
        "bytes": int(bytes_ or 0),
        "errors": int(errors or 0),
        "elapsedTime": float(elapsed or 0.0),
    }


def merge_sync_stats(parts: list[dict[str, int | float]]) -> dict[str, int | float]:
    if not parts:
        return normalize_sync_stats({})
    out = normalize_sync_stats({})
    for part in parts:
        for key in out:
            out[key] = type(out[key])(out[key] + part.get(key, 0))  # type: ignore[operator]
    return out


async def sync_and_stats(
    src_fs: str,
    dst_fs: str,
    *,
    group: str,
    create_empty_src_dirs: bool = True,
    timeout: float = 1800.0,
) -> dict[str, int | float]:
    params: dict[str, Any] = {
        "srcFs": src_fs,
        "dstFs": dst_fs,
        "_async": False,
        "_group": group,
        "createEmptySrcDirs": create_empty_src_dirs,
    }
    await rc_call("sync/sync", params, timeout=timeout)
    raw = await core_stats(group=group)
    return normalize_sync_stats(raw if isinstance(raw, dict) else {})


async def core_bwlimit(rate: str) -> Any:
    return await rc_call("core/bwlimit", {"rate": rate})


async def raw(method: str, params: dict[str, Any] | None = None) -> Any:
    return await rc_call(method, params)


def _default_result(option: dict[str, Any] | None) -> str:
    if not option:
        return ""
    default = option.get("Default")
    if default is None:
        default = option.get("Value")
    if default is True:
        return "true"
    if default is False:
        return "false"
    if default is None:
        return ""
    return str(default)


# rclone asks these after OneDrive OAuth. Empty advanced defaults must not
# skip them — without drive_id/drive_type the remote cannot list files.
_ONEDRIVE_DRIVE_KEYS = {"drive_id", "drive_type"}
_MISSING_DRIVE_ERR = "unable to get drive_id and drive_type"


def _should_autorespond(option: dict[str, Any] | None, ask_all: bool) -> str | None:
    """Return an automatic answer, or None if the user must see the question."""
    if not option:
        return None
    name = str(option.get("Name") or "")
    if name in rclone_oauth.SKIP_OPTION_NAMES:
        return "false"
    if name == "config_fs_advanced":
        return "true" if ask_all else "false"
    # Yes: keep rclone's public Drive client so non-technical users can continue.
    if name == "config_shared_client_id":
        return "true"
    if name == "scope" and not ask_all:
        current = _default_result(option)
        if current:
            return current
        examples = option.get("Examples") or []
        if examples:
            return str(examples[0].get("Value") or "drive")
        return "drive"
    if name in rclone_oauth.OAUTH_OPTION_NAMES:
        return None
    # Personal / Microsoft 365 OneDrive is the usual case; SharePoint stays
    # available when "Show advanced options" is on.
    if name == "config_type" and not ask_all:
        return "onedrive"
    if name == "config_drive_ok":
        return "true"
    # Never save blank drive identity — rclone 1.64+ will refuse to open the remote.
    if name in _ONEDRIVE_DRIVE_KEYS:
        return None
    if option.get("Advanced") and not ask_all:
        return _default_result(option)
    # First Drive pass: leave client id/secret blank unless rclone marked them required.
    if (
        not ask_all
        and not option.get("Required")
        and name in {"client_id", "client_secret", "service_account_file", "service_account_credentials"}
    ):
        return _default_result(option)
    return None


def _remote_name_from_fs(fs: str) -> str | None:
    text = (fs or "").strip()
    if not text or text.startswith("/"):
        return None
    if ":" not in text:
        return None
    name = text.split(":", 1)[0].strip()
    return name or None


def _needs_onedrive_drive(conf: dict[str, Any] | None) -> bool:
    if not conf or str(conf.get("type") or "") != "onedrive":
        return False
    return not (str(conf.get("drive_id") or "").strip() and str(conf.get("drive_type") or "").strip())


async def ensure_onedrive_drive_ids(name: str) -> dict[str, Any] | None:
    """If a OneDrive remote is missing drive_id/drive_type, look them up and save them."""
    name = name.strip()
    if not name:
        return None
    conf = await get_remote(name)
    if not _needs_onedrive_drive(conf):
        return None
    info = await rclone_oauth.onedrive_me_drive(conf.get("token"))
    params: dict[str, Any] = {
        "drive_id": info["id"],
        "drive_type": info["driveType"],
    }
    if info.get("token") and info["token"] != conf.get("token"):
        params["token"] = info["token"]
    await update_remote(name, params)
    return info


async def _config_create(
    name: str,
    type_: str,
    *,
    continue_from: str | None = None,
    result: str | None = None,
    ask_all: bool = False,
) -> dict[str, Any]:
    opt: dict[str, Any] = {
        "nonInteractive": True,
        "obscure": True,
        "noOutput": True,
        # Always walk standard questions (S3 keys, host, …). Advanced ones
        # are skipped in wizard_step unless the user asked for them.
        "all": True,
    }
    if continue_from is not None:
        opt["continue"] = True
        opt["state"] = continue_from
        opt["result"] = "" if result is None else str(result)
    return await rc_call(
        "config/create",
        {"name": name, "type": type_, "parameters": {}, "opt": opt},
    )


def _wizard_payload(
    data: dict[str, Any],
    type_: str,
    hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    state = data.get("State") or ""
    option = data.get("Option")
    err = data.get("Error") or ""
    done = not state and not option and not err
    payload: dict[str, Any] = {
        "done": done,
        "state": state,
        "option": option,
        "error": err,
        "oauth": None,
    }
    if option and not done:
        payload["oauth"] = rclone_oauth.oauth_from_option(type_, option, hints)
    return payload


async def wizard_step(
    name: str,
    type_: str,
    *,
    state: str | None = None,
    result: str | None = None,
    ask_all: bool = False,
    oauth_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    name = name.strip()
    type_ = type_.strip()
    if not name or not type_:
        raise ValueError("Name and storage type are required.")
    if state:
        data = await _config_create(
            name, type_, continue_from=state, result=result, ask_all=ask_all
        )
    else:
        data = await _config_create(name, type_, ask_all=ask_all)

    for _ in range(40):
        option = data.get("Option") or {}
        auto = _should_autorespond(option, ask_all)
        nxt = data.get("State") or ""
        if auto is None or not nxt or data.get("Error"):
            break
        data = await _config_create(
            name, type_, continue_from=nxt, result=auto, ask_all=ask_all
        )
    payload = _wizard_payload(data, type_, oauth_hints)
    if payload["done"] and type_ == "onedrive":
        try:
            await ensure_onedrive_drive_ids(name)
        except Exception as exc:
            payload["done"] = False
            payload["error"] = str(exc)
    return payload


async def wizard_cancel(name: str) -> dict[str, Any]:
    name = name.strip()
    deleted = False
    if name:
        try:
            await delete_remote(name)
            deleted = True
        except Exception:
            pass
    try:
        await rc_call("config/oauthstop", {})
    except Exception:
        pass
    return {"ok": True, "deleted": deleted}
