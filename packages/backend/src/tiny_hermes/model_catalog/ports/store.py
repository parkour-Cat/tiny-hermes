from typing import Protocol
from uuid import UUID

from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpoint,
    ModelEndpointSpec,
)


class EndpointNameTaken(Exception):
    """Another endpoint already answers to this name."""


class ModelEndpointStore(Protocol):
    """Platform-level persistence for approved model endpoints.

    ``read`` answers for a disabled endpoint too: an Agent Version published
    against one keeps naming it, and refusing to describe it would leave the
    publish check unable to say why it refused.
    """

    async def register(self, spec: ModelEndpointSpec, created_by: UUID) -> ModelEndpoint: ...

    async def read(self, endpoint_id: UUID) -> ModelEndpoint | None: ...

    async def list_active(self) -> list[ModelEndpoint]: ...

    async def update(
        self, endpoint_id: UUID, spec: ModelEndpointSpec
    ) -> ModelEndpoint | None: ...

    async def set_status(
        self, endpoint_id: UUID, status: EndpointStatus
    ) -> ModelEndpoint | None: ...

    async def set_window(
        self, endpoint_id: UUID, context_window: int, max_output_tokens: int
    ) -> ModelEndpoint | None: ...

    async def set_accepts_images(
        self, endpoint_id: UUID, accepts: bool
    ) -> ModelEndpoint | None: ...
