"""The registry of model endpoints a platform administrator has approved.

Platform-scoped on purpose. Technical design §12 gives approval of a private
model endpoint to the platform administrator and leaves a workspace
administrator only the choice among approved ones, so there is no
``workspace_id`` here and no column reserved for one.

Nothing in this module holds a credential. ``credential_ref`` names an
environment variable the deployment provides; the value is read at call time and
written nowhere. That is a real limitation with a real cost — rotating a model
key is a restart, and model credentials are deployment configuration rather than
workspace data — and it is preferred to the alternative available in this slice.
Envelope encryption with a rewrappable KEK is roadmap phase four; a table
holding plaintext, or ciphertext under a key with no rotation path, would read
in a review as a control that exists while not being one.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: An environment variable name, which is all this field is ever allowed to be.
CREDENTIAL_REF = re.compile(r"^[A-Z][A-Z0-9_]*$")


class UsageQuality(StrEnum):
    """How far the Token counts from this endpoint can be trusted.

    ``estimated`` is absent deliberately. Technical design §9.4 admits it only
    from a tokenizer verified to match the model, and none exists here, so
    offering the value would offer a number nothing stands behind.
    """

    PROVIDER = "provider"
    UNAVAILABLE = "unavailable"


class EndpointStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


def credential_ref_is_wellformed(value: str) -> bool:
    return CREDENTIAL_REF.fullmatch(value) is not None


class ModelEndpointSpec(BaseModel):
    """What an administrator supplies to register an endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    context_window: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=10_000_000)
    usage_quality: UsageQuality
    credential_ref: str = Field(min_length=1, max_length=200)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https"):
            raise ValueError("base_url must be http or https")
        if not parts.hostname:
            raise ValueError("base_url must name a host")
        if parts.query or parts.fragment:
            # A base URL carrying a query string is how a credential ends up in
            # a log line, and neither belongs in something paths are joined onto.
            raise ValueError("base_url must carry no query string and no fragment")
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"

    @field_validator("credential_ref")
    @classmethod
    def check_credential_ref(cls, value: str) -> str:
        if not credential_ref_is_wellformed(value):
            raise ValueError(
                "credential_ref names an environment variable "
                "(upper case, digits and underscores), never a credential"
            )
        return value

    @model_validator(mode="after")
    def output_fits_the_context(self) -> "ModelEndpointSpec":
        if self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens cannot exceed context_window")
        return self


@dataclass(frozen=True)
class ModelEndpoint:
    id: UUID
    spec: ModelEndpointSpec
    status: EndpointStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @property
    def is_selectable(self) -> bool:
        return self.status is EndpointStatus.ACTIVE
