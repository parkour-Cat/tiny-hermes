from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tiny_hermes.secrets.domain.envelope import (
    InvalidKek,
    UnwrapFailed,
    decode_kek,
    seal,
)
from tiny_hermes.secrets.domain.envelope import (
    rewrap as rewrap_envelope,
)
from tiny_hermes.secrets.domain.mask import mask_plaintext
from tiny_hermes.secrets.domain.models import (
    SecretRecord,
    SecretScope,
    SecretStatus,
    SecretView,
)
from tiny_hermes.secrets.ports.store import DuplicateSecretName, SecretStore
from tiny_hermes.tenancy.domain.models import Actor, Role

LISTERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER})
WORKSPACE_WRITERS = frozenset({Role.WORKSPACE_ADMIN})


class ForbiddenSecretAction(Exception):
    pass


class KekMissing(Exception):
    pass


class UnknownSecret(Exception):
    pass


class InvalidSecretName(Exception):
    pass


class InvalidSecretPlaintext(Exception):
    pass


class SecretNameTaken(Exception):
    pass


class PreviousKekMissing(Exception):
    pass


@dataclass(frozen=True)
class RewrapResult:
    processed: int
    remaining: int
    current_key_id: str


@dataclass(frozen=True)
class KekSettings:
    current: str
    current_id: str
    previous: str = ""
    previous_id: str = ""


class SecretService:
    def __init__(self, store: SecretStore, kek: KekSettings) -> None:
        self._store = store
        self._kek = kek

    async def create(
        self,
        actor: Actor,
        workspace_id: UUID,
        name: str,
        scope: SecretScope,
        plaintext: str,
        request_id: str,
    ) -> SecretView:
        await self._require_writer(actor, workspace_id, scope, request_id)
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise InvalidSecretName
        if not plaintext:
            raise InvalidSecretPlaintext
        envelope = seal(plaintext.encode("utf-8"), self._current_kek(), self._kek.current_id)
        now = datetime.now(UTC)
        record = SecretRecord(
            id=uuid4(),
            name=normalized,
            scope=scope,
            workspace_id=None if scope is SecretScope.PLATFORM else workspace_id,
            status=SecretStatus.ACTIVE,
            mask=mask_plaintext(plaintext),
            ciphertext=envelope.ciphertext,
            nonce=envelope.nonce,
            wrapped_dek=envelope.wrapped_dek,
            wrap_nonce=envelope.wrap_nonce,
            key_id=envelope.key_id,
            created_at=now,
            updated_at=now,
        )
        try:
            stored = await self._store.create(record)
        except DuplicateSecretName as error:
            raise SecretNameTaken from error
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="secret.created",
            resource_id=stored.id,
            request_id=request_id,
            context={"scope": scope.value, "name": stored.name},
        )
        return _view(stored)

    async def list(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> list[SecretView]:
        await self._require_lister(actor, workspace_id, request_id)
        return [_view(record) for record in await self._store.list_visible(workspace_id)]

    async def disable(
        self,
        actor: Actor,
        workspace_id: UUID,
        secret_id: UUID,
        request_id: str,
    ) -> SecretView:
        record = await self._visible(workspace_id, secret_id)
        await self._require_writer(actor, workspace_id, record.scope, request_id)
        disabled = await self._store.disable(secret_id)
        if disabled is None:
            raise UnknownSecret
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="secret.disabled",
            resource_id=disabled.id,
            request_id=request_id,
        )
        return _view(disabled)

    async def rewrap(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> RewrapResult:
        if actor.is_service_account or not actor.is_platform_admin:
            raise ForbiddenSecretAction
        current = self._current_kek()
        previous = self._previous_kek()
        processed = 0
        for record in await self._store.list_by_key_id(self._kek.previous_id):
            try:
                rotated = rewrap_envelope(
                    record.envelope(), previous, current, self._kek.current_id
                )
            except UnwrapFailed:
                continue
            await self._store.replace_wrap(
                record.id, rotated.wrapped_dek, rotated.wrap_nonce, rotated.key_id
            )
            processed += 1
        remaining = await self._store.count_by_key_id(self._kek.previous_id)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="secret.rewrapped",
            resource_id=workspace_id,
            request_id=request_id,
            context={
                "processed": str(processed),
                "remaining": str(remaining),
                "current_key_id": self._kek.current_id,
            },
        )
        return RewrapResult(
            processed=processed,
            remaining=remaining,
            current_key_id=self._kek.current_id,
        )

    async def _visible(self, workspace_id: UUID, secret_id: UUID) -> SecretRecord:
        record = await self._store.get(secret_id)
        if record is None:
            raise UnknownSecret
        if record.scope is SecretScope.PLATFORM:
            return record
        if record.workspace_id != workspace_id:
            raise UnknownSecret
        return record

    def _current_kek(self) -> bytes:
        if not self._kek.current:
            raise KekMissing
        try:
            return decode_kek(self._kek.current)
        except InvalidKek as error:
            raise KekMissing from error

    def _previous_kek(self) -> bytes:
        if not self._kek.previous or not self._kek.previous_id:
            raise PreviousKekMissing
        try:
            return decode_kek(self._kek.previous)
        except InvalidKek as error:
            raise PreviousKekMissing from error

    async def _require_lister(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed=LISTERS,
            audit_as_platform="secret.list_by_platform_admin",
        )

    async def _require_writer(
        self,
        actor: Actor,
        workspace_id: UUID,
        scope: SecretScope,
        request_id: str,
    ) -> None:
        if actor.is_service_account:
            raise ForbiddenSecretAction
        if scope is SecretScope.PLATFORM:
            if not actor.is_platform_admin:
                raise ForbiddenSecretAction
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="secret.platform_write",
                resource_id=workspace_id,
                request_id=request_id,
            )
            return
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed=WORKSPACE_WRITERS,
            audit_as_platform="secret.write_by_platform_admin",
        )

    async def _require_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        *,
        allowed: frozenset[Role],
        audit_as_platform: str,
    ) -> None:
        if actor.is_service_account:
            raise ForbiddenSecretAction
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenSecretAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenSecretAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=audit_as_platform,
            resource_id=workspace_id,
            request_id=request_id,
        )


def _view(record: SecretRecord) -> SecretView:
    return SecretView(
        id=record.id,
        name=record.name,
        scope=record.scope,
        workspace_id=record.workspace_id,
        status=record.status,
        mask=record.mask,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
