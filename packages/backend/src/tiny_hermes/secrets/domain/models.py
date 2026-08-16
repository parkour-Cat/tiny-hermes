from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.secrets.domain.envelope import Envelope


class SecretScope(StrEnum):
    WORKSPACE = "workspace"
    PLATFORM = "platform"


class SecretStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True)
class SecretView:
    """What HTTP may return: identity and a mask, never plaintext."""

    id: UUID
    name: str
    scope: SecretScope
    workspace_id: UUID | None
    status: SecretStatus
    mask: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SecretRecord:
    """A stored envelope. Ciphertext lives here; plaintext never does."""

    id: UUID
    name: str
    scope: SecretScope
    workspace_id: UUID | None
    status: SecretStatus
    mask: str
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    wrap_nonce: bytes
    key_id: str
    created_at: datetime
    updated_at: datetime

    def envelope(self) -> Envelope:
        return Envelope(
            ciphertext=self.ciphertext,
            nonce=self.nonce,
            wrapped_dek=self.wrapped_dek,
            wrap_nonce=self.wrap_nonce,
            key_id=self.key_id,
        )
