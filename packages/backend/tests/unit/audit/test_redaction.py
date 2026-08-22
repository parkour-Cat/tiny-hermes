"""§2: redaction acts on `context` alone — the only `audit_events` column
that can hold business data (plan §36-51, product design §9 item 8: "审计记录
只保留业务所需的脱敏信息"). Every other column is an identifier.

**Whitelist, not blacklist** (plan §8's own first decision). The required
test is the one the plan names explicitly: register nothing, and an
unregistered key does not survive — not the value, not the key's name.
"""

from tiny_hermes.audit.domain.redaction import redact_context


def test_an_unregistered_key_does_not_survive_at_all() -> None:
    """The plan's own exit check for §2, verbatim: add a key nobody
    whitelisted, and a viewer's read does not contain it — key or value."""
    redacted = redact_context({"session_summary": "the customer threatened to leave"})

    assert "session_summary" not in redacted
    assert "the customer threatened to leave" not in redacted.values()
    assert "the customer threatened to leave" not in str(redacted)


def test_default_whitelist_is_conservative_by_construction() -> None:
    """No key in this codebase has been reviewed and declared safe for a
    脱敏 (desensitized) reader yet — see the module docstring for why an
    empty default is the honest starting point rather than a guess dressed
    up as a decision. Whoever needs a specific key visible registers it
    deliberately; nothing here does that on their behalf."""
    from tiny_hermes.audit.domain.redaction import VIEWER_CONTEXT_WHITELIST

    assert VIEWER_CONTEXT_WHITELIST == frozenset()


def test_a_registered_key_survives_with_its_value_intact() -> None:
    """The whitelist mechanism itself works in both directions — this is
    the allow path, proven with a caller-supplied whitelist so the test does
    not depend on which keys are registered by default today."""
    redacted = redact_context(
        {"outcome": "approved", "session_summary": "secret"},
        whitelist=frozenset({"outcome"}),
    )

    assert redacted == {"outcome": "approved"}


def test_redaction_never_mutates_the_original_context() -> None:
    original = {"session_summary": "secret"}

    redact_context(original)

    assert original == {"session_summary": "secret"}


def test_empty_context_redacts_to_empty() -> None:
    assert redact_context({}) == {}
