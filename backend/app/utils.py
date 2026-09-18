from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "app") -> str:
    text = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return text[:48] or fallback


def write_dotenv(path: str | Path, values: dict[str, str], example: str | Path | None = None) -> None:
    """Write a .env file, overlaying `values` onto an existing skeleton.

    If the destination already exists, that file is the skeleton (production
    mail, admin, and app keys stay). The example file is only used when there
    is no .env yet. Filling holes from .env.example used to wipe live SMTP.
    """
    dest = Path(path)
    example_path = Path(example) if example else None
    if dest.exists():
        template = dest
    elif example_path and example_path.exists():
        template = example_path
    else:
        template = None
    lines: list[str] = []
    seen: set[str] = set()
    if template is not None:
        for line in template.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                lines.append(line)
                continue
            key, _, _ = line.partition("=")
            key = key.strip()
            if key in values:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(line)
                seen.add(key)
        if any(k not in seen for k in values):
            lines.append("")
            lines.append("# Set by VPS Dashboard")
    else:
        lines.append("# Set by VPS Dashboard")
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")


def parse_dotenv(path: str | Path) -> dict[str, str]:
    result: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value
    return result


SENSITIVE_ENV_KEYS = {
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "KEY",
    "PRIVATE",
    "CREDENTIAL",
    "AUTH",
}


def redact_env(env: list[str] | dict[str, str] | None) -> list[str] | dict[str, str]:
    if env is None:
        return []
    if isinstance(env, dict):
        out = {}
        for k, v in env.items():
            upper = k.upper()
            if any(s in upper for s in SENSITIVE_ENV_KEYS):
                out[k] = "***"
            else:
                out[k] = v
        return out
    out_list = []
    for item in env:
        if "=" not in item:
            out_list.append(item)
            continue
        k, _, v = item.partition("=")
        upper = k.upper()
        if any(s in upper for s in SENSITIVE_ENV_KEYS):
            out_list.append(f"{k}=***")
        else:
            out_list.append(item)
    return out_list
