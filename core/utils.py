import os
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Windows forbids a few characters in filenames; also strip ASCII control chars (0x00-0x1F).
_windows_bad = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def safe_filename(name: str, default: str = "file") -> str:
    name = os.path.basename(name or "")
    name = _windows_bad.sub("_", name).strip(" .")
    if not name:
        name = default
    return name


def safe_return_to(raw: str | None, default: str = "/dashboard") -> str:
    """Protect against open-redirect: allow only local absolute paths like /dashboard#cat-1."""
    if not raw:
        return default
    raw = raw.strip()
    if not raw or len(raw) > 2048:
        return default

    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return default
    if not parts.path.startswith("/"):
        return default
    return raw


def set_qp(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[key] = value
    return urlunsplit(("", "", parts.path, urlencode(q, doseq=True), parts.fragment))


def normalize_notify_chat_id(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip().replace(" ", "")
    if not s:
        return None
    if s.startswith("-"):
        return s
    if s.isdigit():
        return "-" + s
    return s
