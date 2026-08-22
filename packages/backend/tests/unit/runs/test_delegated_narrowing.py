"""§13's sixth clause at runtime: a child gets the intersection, not its spec.

Publish-time validation already refuses a delegation that offers a child more
than the parent holds (`DelegationTooWide`). That is not the same guard as
this one, and the difference is why both exist: `delegation_scope` is a
**snapshot taken when the delegation happened**, while the child's own
published spec is whatever it is. A child whose spec lists a tool the
snapshot does not name must still not get it — publish-time validation never
looked at that pairing, because at publish time there was no delegation yet.

These properties had **no tests at all** before this file. `delegated_scope`
appeared in zero test files, which for the mechanism §13's whole security
argument rests on is not a defensible place to leave it — every child in the
integration suite binds nothing, so the intersection was only ever exercised
in the case where it has nothing to do.
"""

from tiny_hermes.agents.domain.delegation import DelegationScope
from tiny_hermes.runs.ports.store import ExecutionContext


def _context(
    *, spec_tools: tuple[str, ...], scope: DelegationScope | None
) -> ExecutionContext:
    class Spec:
        tools = spec_tools
        secrets: tuple[str, ...] = ()
        credential_ref = None

    context = ExecutionContext.__new__(ExecutionContext)
    object.__setattr__(context, "spec", Spec())
    object.__setattr__(context, "delegated_scope", scope)
    return context


def test_a_run_that_was_not_delegated_keeps_its_whole_spec() -> None:
    """The ordinary Run. `None` means "nobody narrowed this", which is not
    the same as an empty scope and must not be read as one."""
    context = _context(spec_tools=("file.read", "shell.exec"), scope=None)

    assert context.tools == ("file.read", "shell.exec")


def test_a_child_keeps_only_what_the_delegation_also_named() -> None:
    context = _context(
        spec_tools=("file.read", "shell.exec"),
        scope=DelegationScope(tools=frozenset({"file.read"})),
    )

    assert context.tools == ("file.read",)


def test_a_delegation_that_named_nothing_grants_nothing() -> None:
    """The case every child in the integration suite is in, and the reason
    that suite could never have caught a broken intersection: a scope naming
    no tools must leave the child with none, however many its own spec has.

    An implementation that treated the empty set as "unrestricted" would
    pass every existing test in this repository and hand every child its
    parent's entire toolbox.
    """
    context = _context(
        spec_tools=("file.read", "shell.exec"), scope=DelegationScope()
    )

    assert context.tools == ()


def test_the_delegation_cannot_widen_past_the_spec() -> None:
    """An intersection, not a replacement. A snapshot naming a tool this
    Agent's own version does not have must not conjure it — the scope
    narrows what the spec allows and can never be the only thing consulted.
    """
    context = _context(
        spec_tools=("file.read",),
        scope=DelegationScope(tools=frozenset({"file.read", "shell.exec"})),
    )

    assert context.tools == ("file.read",)


def test_order_comes_from_the_spec_rather_than_the_scope() -> None:
    """`DelegationScope` holds frozensets, whose iteration order is not
    stable. Reading them into the result would make the tool list the model
    is shown vary between runs of the same Agent, and `§10.2`'s two steps
    agree only because they read one ordered thing."""
    context = _context(
        spec_tools=("a.one", "b.two", "c.three"),
        scope=DelegationScope(tools=frozenset({"c.three", "a.one", "b.two"})),
    )

    assert context.tools == ("a.one", "b.two", "c.three")
