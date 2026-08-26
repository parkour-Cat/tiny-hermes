"""The registry of model endpoints a platform administrator has approved.

Platform-scoped on purpose. Technical design §12 gives approval of a private
model endpoint to the platform administrator and leaves a workspace
administrator only the choice among approved ones, so there is no
``workspace_id`` here and no column reserved for one.

Nothing in this module holds a credential. ``credential_ref`` names an
environment variable the deployment provides, or the id of an active Secret.
The value is read at call time and written nowhere.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: An environment variable name. A Secret id is a UUID and is accepted separately.
CREDENTIAL_REF = re.compile(r"^[A-Z][A-Z0-9_]*$")


class UsageQuality(StrEnum):
    """How far the Token counts from this endpoint can be trusted.

    ``estimated`` is absent deliberately. Technical design §9.4 admits it only
    from a tokenizer verified to match the model, and none exists here, so
    offering the value would offer a number nothing stands behind.
    """

    PROVIDER = "provider"
    UNAVAILABLE = "unavailable"


class ContextAccounting(StrEnum):
    """How an endpoint's window is shared between input and reserved output.

    ``shared`` means one window covers both, so the budget planner must
    subtract what it reserves for the answer before it decides what to send.
    ``separate`` means the endpoint bounds them independently.
    """

    SHARED = "shared"
    SEPARATE = "separate"


class EndpointStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


def credential_ref_is_wellformed(value: str) -> bool:
    if CREDENTIAL_REF.fullmatch(value) is not None:
        return True
    try:
        UUID(value)
    except ValueError:
        return False
    return True


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
    #: Whether the reserved output competes with the input for one window.
    #: Product design §7.4.2 requires this be declared per endpoint and
    #: computed from the declaration, never guessed from a provider name — the
    #: two answers differ by the whole reserved output. ``shared`` is the
    #: default because it is the conservative one: an endpoint that is really
    #: `separate` gets a smaller request than it had to take, while the
    #: opposite mistake gets a request refused.
    context_accounting: ContextAccounting = ContextAccounting.SHARED
    #: The local tokenizer the budget planner should use for this endpoint.
    #: Recorded even though no tokenizer ships verified in this release: the
    #: planner falls back to a conservative character bound and says so, and
    #: §9.4 admits a real estimate only from a tokenizer verified against the
    #: model. A name here is a declaration, never a promise the count is exact.
    tokenizer: str | None = Field(default=None, max_length=64)
    #: Whether this endpoint accepts image input.
    #:
    #: Declared, never inferred from `model`. §7.4.2 applies the same rule to
    #: `context_accounting` and the reason holds here: DeepSeek's vision
    #: support is a *different model id* from its text one
    #: (`deepseek-v4-flash-vision-exp` beside `deepseek-v4-flash`), so a
    #: name-sniffing check would be a guess that goes silently wrong the next
    #: time a vendor renames something.
    #:
    #: `False` by default, which is what every endpoint registered before
    #: this field existed actually is.
    accepts_images: bool = False

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
                "(upper case, digits and underscores) or a Secret id, never a credential"
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
