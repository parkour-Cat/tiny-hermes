"""Where the Controller is, as one URL (tier B design §4.1).

A URL rather than a bare path so that a later scheme — `tls://` for a
Controller on another host — is a new value of the same setting instead of a
second setting that has to be kept consistent with the first. Until the
transport speaks TLS, `unix://` is the only scheme accepted: one that parsed
and was then served over the local socket anyway would let an operator believe
a remote Controller was in use.

Pure, like `frames.py`, so the rules are testable without a socket.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ControllerAddress:
    path: str

    @classmethod
    def parse(cls, value: str) -> "ControllerAddress":
        parts = urlsplit(value)
        if parts.scheme == "tcp":
            # The Controller holds the Docker socket. A way in that nothing
            # authenticates is a way to root on its host.
            raise ValueError(
                f"{value!r}: plain TCP is never accepted; a remote controller is tls://"
            )
        if parts.scheme == "tls":
            raise ValueError(f"{value!r}: tls:// is not supported yet")
        if parts.scheme != "unix":
            raise ValueError(f"{value!r}: expected unix:///absolute/path")
        if parts.netloc or parts.query or parts.fragment or not parts.path.startswith("/"):
            raise ValueError(f"{value!r}: expected unix:///absolute/path")
        return cls(path=parts.path)

    def __str__(self) -> str:
        return f"unix://{self.path}"
