"""Resolve a model endpoint's credential at call time.

`credential_ref` names either an environment variable (the original 3A form) or
the id of an active Secret. The value is read at call time and stored nowhere
on the endpoint. A Secret unwrap that fails does not rewrite the row.
"""

from __future__ import annotations

import os
from uuid import UUID

from tiny_hermes.model_catalog.domain.models import CREDENTIAL_REF
from tiny_hermes.secrets.domain.envelope import UnwrapFailed, unseal
from tiny_hermes.secrets.domain.models import SecretStatus
from tiny_hermes.secrets.ports.store import SecretStore


class CredentialMissing(Exception):
    """The named environment variable is unset, or the Secret cannot be unwrapped.

    Raised at registration as well as at call time, so a deployment that forgot
    to supply a key is found by the administrator who registered the endpoint
    rather than by whoever happened to submit the first Run.
    """

    def __init__(self, ref: str) -> None:
        super().__init__(f"the credential named by {ref} is not available")
        self.ref = ref


def secret_id_from_ref(ref: str) -> UUID | None:
    """A Secret id if `ref` is a UUID; otherwise this is still an env-var name.

    Environment-variable grammar cannot contain hyphens, so a UUID never collides
    with a well-formed env name.
    """
    if CREDENTIAL_REF.fullmatch(ref):
        return None
    try:
        return UUID(ref)
    except ValueError:
        return None


def resolve(ref: str) -> str:
    value = os.environ.get(ref)
    if not value:
        raise CredentialMissing(ref)
    return value


def is_available(ref: str) -> bool:
    return bool(os.environ.get(ref))


class CredentialResolver:
    def __init__(self, secrets: SecretStore | None, kek: bytes | None) -> None:
        self._secrets = secrets
        self._kek = kek

    async def is_available(self, ref: str) -> bool:
        secret_id = secret_id_from_ref(ref)
        if secret_id is None:
            return is_available(ref)
        if self._secrets is None:
            return False
        record = await self._secrets.get(secret_id)
        return record is not None and record.status is SecretStatus.ACTIVE

    async def resolve(self, ref: str) -> str:
        secret_id = secret_id_from_ref(ref)
        if secret_id is None:
            return resolve(ref)
        if self._secrets is None:
            raise CredentialMissing(ref)
        record = await self._secrets.get(secret_id)
        if record is None or record.status is not SecretStatus.ACTIVE:
            raise CredentialMissing(ref)
        if self._kek is None:
            raise CredentialMissing(ref)
        try:
            return unseal(record.envelope(), self._kek).decode("utf-8")
        except UnwrapFailed as error:
            raise CredentialMissing(ref) from error
        except UnicodeDecodeError as error:
            raise CredentialMissing(ref) from error
