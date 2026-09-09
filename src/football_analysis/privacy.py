from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "mobile",
    "openid",
    "password",
    "phone",
    "refresh_token",
    "secret",
    "token",
    "unionid",
}


def _is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith("_token")


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, "[REDACTED]" if _is_sensitive(key) else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value

