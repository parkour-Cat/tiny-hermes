"""What a published site may contain, and how its links and passes are made.

Design: docs/superpowers/specs/2026-10-25-site-publishing-design.md.
"""

import base64
import hashlib
import hmac
import json
import posixpath
import secrets
from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from tiny_hermes.session_workspace.domain.models import EntryType, WorkspaceEntry

MAX_FILES = 500
MAX_TOTAL_BYTES = 50 * 1024 * 1024

#: Extension to media type. A file outside this map is refused at publish
#: time, never served as something the browser guesses.
MEDIA_TYPES = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "json": "application/json",
    "map": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "xml": "application/xml",
}


class SiteRefused(Exception):
    """A directory that cannot be published, and why, for the model to fix."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def media_type(path: str) -> str:
    return MEDIA_TYPES[_extension(path)]


def _root(raw: str) -> str:
    """The root relative to the data mount, or a refusal if it leaves it."""
    if "\\" in raw:
        raise SiteRefused("bad_path", f"{raw!r} is not a workspace path")
    stripped = raw.strip().lstrip("/") or "."
    if any(part == ".." for part in stripped.split("/")):
        raise SiteRefused("bad_path", f"{raw!r} leaves the workspace")
    normalized = posixpath.normpath(stripped)
    return "" if normalized == "." else normalized


def select_site(entries: Iterable[WorkspaceEntry], root: str) -> dict[str, WorkspaceEntry]:
    """The files under `root`, keyed by their path inside it; refused whole or not at all."""
    base = _root(root)
    prefix = f"{base}/" if base else ""
    files = {
        entry.path[len(prefix):]: entry
        for entry in entries
        if entry.entry_type is EntryType.FILE and entry.path.startswith(prefix)
    }
    if not files:
        raise SiteRefused("not_found", f"no files under {base or '.'!r}")
    if "index.html" not in files:
        raise SiteRefused("no_index", f"{base or '.'!r} has no index.html")
    if len(files) > MAX_FILES:
        raise SiteRefused("too_many_files", f"{len(files)} files, at most {MAX_FILES}")
    total = sum(entry.size for entry in files.values())
    if total > MAX_TOTAL_BYTES:
        raise SiteRefused("too_large", f"{total} bytes, at most {MAX_TOTAL_BYTES}")
    foreign = sorted(path for path in files if _extension(path) not in MEDIA_TYPES)
    if foreign:
        shown = ", ".join(foreign[:20]) + (" ..." if len(foreign) > 20 else "")
        raise SiteRefused("file_type", f"not a static site file: {shown}")
    return files


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """What is stored: a database read does not hand out working links."""
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, purpose: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), f"{purpose}.{body}".encode(), hashlib.sha256).digest())


def sign(secret: str, purpose: str, claims: dict[str, str], expires_at: datetime) -> str:
    """`claims` and an expiry, HMAC-signed for one purpose.

    The purpose is under the MAC, so a twelve-hour pass cannot be presented
    where a one-minute ticket is asked for.
    """
    body = _b64(json.dumps({"c": claims, "e": int(expires_at.timestamp())}).encode())
    return f"{body}.{_mac(secret, purpose, body)}"


def verify(secret: str, purpose: str, value: str, now: datetime) -> dict[str, str] | None:
    """The claims of a valid, unexpired value for this purpose, or None."""
    body, _, mac = value.partition(".")
    if not body or not mac or "." in mac:
        return None
    if not hmac.compare_digest(mac, _mac(secret, purpose, body)):
        return None
    try:
        document: Any = json.loads(_unb64(body))
        claims = document["c"]
        expires = int(document["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(claims, dict) or now.timestamp() > expires:
        return None
    pairs = cast(dict[Any, Any], claims)
    return {str(key): str(item) for key, item in pairs.items()}


def security_headers() -> dict[str, str]:
    """A site may run its scripts and load its own files, and reach nothing else."""
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        # Revalidated every time: a revoked site must not live on in a cache.
        "Cache-Control": "private, no-cache",
    }


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, (parts.hostname or "").lower(), port


def same_origin(first: str, second: str) -> bool:
    return _origin(first) == _origin(second)


def same_host(first: str, second: str) -> bool:
    """Whether two URLs share a host, which is what cookies go by: not the port."""
    return _origin(first)[1] == _origin(second)[1]
