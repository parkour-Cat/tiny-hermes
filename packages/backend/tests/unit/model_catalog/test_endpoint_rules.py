import pytest
from pydantic import ValidationError
from tiny_hermes.model_catalog.domain.models import (
    ModelEndpointSpec,
    UsageQuality,
    credential_ref_is_wellformed,
)


def spec(**overrides: object) -> ModelEndpointSpec:
    fields: dict[str, object] = {
        "name": "acme-gpt",
        "kind": "openai_compatible",
        "base_url": "https://models.example.com/v1",
        "model": "acme-large",
        "context_window": 128_000,
        "max_output_tokens": 4_096,
        "usage_quality": "provider",
        "credential_ref": "TINY_HERMES_MODEL_KEY_ACME",
    }
    fields.update(overrides)
    return ModelEndpointSpec.model_validate(fields)


def test_a_well_formed_endpoint_validates() -> None:
    endpoint = spec()
    assert endpoint.usage_quality is UsageQuality.PROVIDER
    assert endpoint.base_url == "https://models.example.com/v1"


def test_a_trailing_slash_is_normalized_away() -> None:
    """So one endpoint registered twice is one endpoint, and paths join predictably."""
    assert spec(base_url="https://models.example.com/v1/").base_url == (
        "https://models.example.com/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://models.example.com",
        "file:///etc/passwd",
        "models.example.com/v1",
        "https://",
        "http://models.example.com/v1?key=secret",
        "https://models.example.com/v1#fragment",
    ],
)
def test_an_unusable_base_url_is_refused(url: str) -> None:
    """A query string or fragment in a base URL is a credential in a URL waiting to happen."""
    with pytest.raises(ValidationError):
        spec(base_url=url)


def test_plaintext_is_accepted_by_the_schema_and_judged_at_call_time() -> None:
    """`http` is not a schema error.

    Whether plaintext is allowed depends on where the address lands, which the
    schema cannot know. The outbound client refuses it unless the address is
    inside an approved range, and refusing it twice in two places with two
    different rules is how the two drift apart.
    """
    assert spec(base_url="http://models.internal/v1").base_url == "http://models.internal/v1"


def test_estimated_usage_quality_is_refused() -> None:
    """Deliberate, not forgotten.

    Technical design §9.4 permits an estimate only from a tokenizer verified to
    match the model. There is no such tokenizer here, so the value would be a
    number the platform cannot stand behind.
    """
    with pytest.raises(ValidationError):
        spec(usage_quality="estimated")


@pytest.mark.parametrize(
    ("ref", "wellformed"),
    [
        ("TINY_HERMES_MODEL_KEY_ACME", True),
        ("A", True),
        ("A1_B2", True),
        ("lowercase", False),
        ("1LEADING_DIGIT", False),
        ("HAS-HYPHEN", False),
        ("HAS SPACE", False),
        ("", False),
        ("sk-a-real-looking-key", False),
        ("11111111-2222-4333-8444-555555555555", True),
    ],
)
def test_a_credential_ref_names_an_environment_variable_or_a_secret_id(
    ref: str, wellformed: bool
) -> None:
    """It is a name, never a value.

    The shape is checked so that pasting the key itself into the field is caught
    here rather than stored, read back, and wondered about later.
    """
    assert credential_ref_is_wellformed(ref) is wellformed


def test_a_credential_shaped_ref_is_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        spec(credential_ref="sk-a-real-looking-key")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_window", 0),
        ("context_window", -1),
        ("max_output_tokens", 0),
        ("name", ""),
        ("model", ""),
        ("kind", "anthropic_messages"),
    ],
)
def test_unusable_fields_are_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        spec(**{field: value})


def test_max_output_cannot_exceed_the_context_window() -> None:
    """An endpoint that cannot produce what it claims is a misconfiguration, not a limit."""
    with pytest.raises(ValidationError):
        spec(context_window=4_096, max_output_tokens=8_192)


def test_the_spec_holds_no_credential_field_at_all() -> None:
    """The shape of the promise: this platform stores a name and never a secret."""
    assert "credential" not in ModelEndpointSpec.model_fields
    assert "api_key" not in ModelEndpointSpec.model_fields
    assert set(ModelEndpointSpec.model_fields) == {
        "name",
        "kind",
        "base_url",
        "model",
        "context_window",
        "max_output_tokens",
        "usage_quality",
        "credential_ref",
    }
