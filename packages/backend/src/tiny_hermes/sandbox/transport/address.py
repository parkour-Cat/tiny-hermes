"""Where the Controller is, as one URL (tier B design §4.1).

A URL rather than a bare path so that a Controller on another host is a new
value of the same setting instead of a second setting that has to be kept
consistent with the first. Two schemes: `unix://` for the socket in the volume
a single-host stack shares, `tls://` for everything else. Plain `tcp://` is
never accepted.

Pure, like `frames.py`, so the rules are testable without a socket.
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

_UNIX = "expected unix:///absolute/path"
_TLS = "expected tls://host:port"


@dataclass(frozen=True)
class ControllerAddress:
    scheme: Literal["unix", "tls"]
    path: str = ""
    host: str = ""
    port: int = 0

    @property
    def is_tls(self) -> bool:
        return self.scheme == "tls"

    @classmethod
    def parse(cls, value: str) -> "ControllerAddress":
        parts = urlsplit(value)
        if parts.scheme == "tcp":
            # The Controller holds the Docker socket. A way in that nothing
            # authenticates is a way to root on its host.
            raise ValueError(
                f"{value!r}: plain TCP is never accepted; a remote controller is tls://"
            )
        if parts.query or parts.fragment:
            raise ValueError(f"{value!r}: {_UNIX} or {_TLS}")
        if parts.scheme == "unix":
            if parts.netloc or not parts.path.startswith("/"):
                raise ValueError(f"{value!r}: {_UNIX}")
            return cls(scheme="unix", path=parts.path)
        if parts.scheme == "tls":
            try:
                port = parts.port
            except ValueError as malformed:
                raise ValueError(f"{value!r}: {_TLS}") from malformed
            if (
                not parts.hostname
                or port is None
                or port == 0
                or parts.path
                or parts.username is not None
            ):
                raise ValueError(f"{value!r}: {_TLS}")
            return cls(scheme="tls", host=parts.hostname, port=port)
        raise ValueError(f"{value!r}: {_UNIX} or {_TLS}")

    def __str__(self) -> str:
        if self.scheme == "unix":
            return f"unix://{self.path}"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"tls://{host}:{self.port}"
