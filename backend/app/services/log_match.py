from __future__ import annotations

import re

LOG_PATTERNS = ("error", "fatal", "exception", "panic", "traceback")

# Vite/webpack hashed files such as input-error-DEkipNxA.js, and /build/assets/... URLs.
_LOG_NOISE_RE = re.compile(
    r"""
    (?:https?://[^\s"'<>]*)?/build/assets/[^\s"'<>\\,;]*
    |
    [A-Za-z0-9_./-]*(?:[Ee]rror|ERROR)[A-Za-z0-9_-]*-[A-Za-z0-9_-]{4,}\.(?:js|css|map|mjs)
    """,
    re.VERBOSE,
)
_HTTP_COMBINED_STATUS_RE = re.compile(r"HTTP/\d(?:\.\d)?[\"\s]+(\d{3})\b")
_JSON_STATUS_RE = re.compile(r'"(?:status|status_code|statusCode)"\s*:\s*(\d{3})\b')
# Access-log request targets: GET /.env-traceback should not count as a traceback.
_HTTP_REQUEST_RE = re.compile(
    r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+\s+HTTP/[\d.]+"',
    re.I,
)
_JSON_URI_RE = re.compile(
    r'"(?:uri|url|path|request_uri)"\s*:\s*"(?:\\.|[^"\\])*"',
    re.I,
)
# Real log severity — not the word "error" inside a URL or filename.
_LOG_SEVERITY_RE = re.compile(
    r"""
    (?:^|[\s\[])ERROR(?:[\s\]:]|$)
    | \.(?:ERROR|CRITICAL|ALERT|EMERGENCY):
    | \[error\]
    | \[fatal\]
    | \bfatal\s+error\b
    | \b(?:CRITICAL|EMERGENCY|PANIC)\b
    | \b(?:exception|traceback)\b
    | (?:level|severity)\s*["'=:\s]+\s*["']?(?:error|fatal|critical|panic)
    | http\.log\.error
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _strip_log_noise(line: str) -> str:
    return _LOG_NOISE_RE.sub(" ", line)


def _strip_request_targets(line: str) -> str:
    line = _HTTP_REQUEST_RE.sub(" ", line)
    return _JSON_URI_RE.sub(" ", line)


def _http_status(line: str) -> int | None:
    match = _HTTP_COMBINED_STATUS_RE.search(line) or _JSON_STATUS_RE.search(line)
    return int(match.group(1)) if match else None


def _has_error_keyword(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in LOG_PATTERNS)


def log_line_is_error(line: str) -> bool:
    """Return True only for likely application/server errors, not access-log noise."""
    if not line or not line.strip():
        return False

    status = _http_status(line)
    cleaned = _strip_request_targets(_strip_log_noise(line))

    if status is not None:
        if 500 <= status <= 599:
            return True
        # 2xx/3xx/4xx access logs often mention files like input-error-*.js.
        return bool(_LOG_SEVERITY_RE.search(cleaned))

    return _has_error_keyword(cleaned)
