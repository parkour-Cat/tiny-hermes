"""Read model ids without treating provider response bodies as console content."""

import asyncio
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec, credential_ref_is_wellformed
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError
from tiny_hermes.shared.errors import AppError


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    api_key: SecretStr | None = None
    credential_ref: str | None = Field(default=None, max_length=200)

    @field_validator("base_url")
    @classmethod
    def address(cls, value: str) -> str:
        normalized = ModelEndpointSpec.normalize_base_url(value)
        parts = urlsplit(normalized)
        if parts.username is not None or parts.password is not None:
            raise ValueError("Credentials belong in API Key, not in the URL")
        if len(parts.hostname or "") > 253:
            raise ValueError("Host is too long")
        return normalized

    @model_validator(mode="after")
    def credential(self) -> "DiscoveryRequest":
        if (self.api_key is None) == (self.credential_ref is None):
            raise ValueError("Supply exactly one credential")
        if self.api_key is not None:
            key = self.api_key.get_secret_value()
            if not key.strip() or len(key) > 16384 or "\r" in key or "\n" in key:
                raise ValueError("Invalid API key")
        if self.credential_ref is not None and not credential_ref_is_wellformed(
            self.credential_ref
        ):
            raise ValueError("Invalid credential reference")
        return self


class ModelId(BaseModel):
    id: str = Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f]+$")


class ModelList(BaseModel):
    data: list[ModelId] = Field(max_length=5000)


class DiscoveryResponse(BaseModel):
    models: list[Annotated[str, Field(max_length=200)]]


def discovery_error(code: str) -> AppError:
    return AppError(
        code=code,
        title="Model discovery failed",
        status=422,
        detail="Could not obtain the model list. Check the connection or enter a model name.",
    )


async def discover_models(
    client: SafeOutboundClient, base_url: str, token: str
) -> DiscoveryResponse:
    try:
        async with asyncio.timeout(25):
            response = await client.request(
                "GET", f"{base_url}/models", headers={"Authorization": f"Bearer {token}"}
            )
    except (OutboundError, TimeoutError):
        raise discovery_error("model_discovery_failed") from None
    if response.status_code in (401, 403):
        raise discovery_error("model_discovery_unauthorized")
    if response.status_code in (404, 405):
        raise discovery_error("model_discovery_unsupported")
    if response.status_code != 200:
        raise discovery_error("model_discovery_failed")
    try:
        result = ModelList.model_validate_json(response.content)
    except ValueError:
        raise discovery_error("model_discovery_invalid") from None
    return DiscoveryResponse(models=list(dict.fromkeys(model.id for model in result.data)))
