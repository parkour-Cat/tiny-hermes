"""What a child Agent may do, and the one direction this module moves in.

Product design §13. A child's permissions are the **intersection** of what its
parent holds and what the delegation asked for, across six faces: tools, files,
network, secrets, skills and memory. Nothing here widens anything, and that is
not a rule somebody has to follow — there is no function, argument or
combination that returns more than the parent had.

Two things follow from that and are worth reading twice.

**An empty face is empty, never everything.** The same decision
`outbound/domain/scope.py` makes for the same reason: a missing layer is what a
bug looks like, and a vacuous "all" turns it into an open door. A parent that
holds no secrets delegates no secrets, and a delegation that names no tools
gets no tools.

**Memory is two permissions, not one.** §13's fifth clause splits reading a
private memory from proposing one, because a child that may read what somebody
said is not thereby a child that may write down conclusions about them. And the
scope a child reads is its **own** — `workspace + child agent + end user` —
which is a property of `MemoryScope` rather than of anything here; this module
only decides whether it may read at all.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tiny_hermes.agents.domain.models import AgentSpec, ChildBinding

#: §13's third clause as a number: an Agent at this depth may not delegate.
#: One level, and that is the design rather than a starting point — a tree of
#: arbitrary depth is a different product with different budget, cancellation
#: and permission questions, and none of them are answered here.
MAX_DELEGATION_DEPTH = 1


class MemoryPermission(StrEnum):
    """§13's two memory verbs, kept apart on purpose."""

    #: May read private memories in its own scope.
    READ_PRIVATE = "memory.read_private"
    #: May offer a candidate in its own scope. Still subject to §14.1's policy:
    #: this permits asking, never writing.
    PROPOSE_PRIVATE = "memory.propose_private"


@dataclass(frozen=True)
class DelegationScope:
    """The six faces, each a set of names.

    Frozen sets rather than lists: a face is a membership question, and an order
    here would be an order somebody eventually depended on.
    """

    tools: frozenset[str] = frozenset()
    #: Artifact ids the child may read. Never paths — §13's eighth clause is
    #: that files move as authorizations, and a path would be the shape of a
    #: shared directory.
    files: frozenset[str] = frozenset()
    #: Outbound entries, as text. Measured against the real four-layer scope at
    #: call time; this face only decides which of the parent's the child keeps.
    network: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()
    #: Skill *version* ids, never names — the same rule the Agent spec keeps.
    skills: frozenset[str] = frozenset()
    memory: frozenset[MemoryPermission] = frozenset()

    @classmethod
    def of(
        cls,
        *,
        tools: Iterable[str] = (),
        files: Iterable[str] = (),
        network: Iterable[str] = (),
        secrets: Iterable[str] = (),
        skills: Iterable[str] = (),
        memory: Iterable[MemoryPermission | str] = (),
    ) -> "DelegationScope":
        return cls(
            tools=frozenset(tools),
            files=frozenset(files),
            network=frozenset(network),
            secrets=frozenset(secrets),
            skills=frozenset(skills),
            memory=frozenset(MemoryPermission(item) for item in memory),
        )

    @property
    def empty(self) -> bool:
        """Nothing on any face.

        Read as a word rather than as six `not` checks, because "this delegation
        grants nothing" is the sentence a refusal is written in.
        """
        return not (
            self.tools
            or self.files
            or self.network
            or self.secrets
            or self.skills
            or self.memory
        )

    def covers(self, other: "DelegationScope") -> bool:
        """Whether every face of `other` is inside this one.

        The publish-time question: an author may not delegate what their own
        Agent does not hold. Answered face by face rather than by a single
        subset test, so a new face added later cannot be forgotten silently —
        it fails to compile here first.
        """
        return (
            other.tools <= self.tools
            and other.files <= self.files
            and other.network <= self.network
            and other.secrets <= self.secrets
            and other.skills <= self.skills
            and other.memory <= self.memory
        )

    def missing_from(self, other: "DelegationScope") -> dict[str, tuple[str, ...]]:
        """What `other` asked for that this scope does not hold, by face.

        For a refusal an author can act on. A publish that only said "too wide"
        would send them guessing across six faces.
        """
        found: dict[str, tuple[str, ...]] = {}
        for face, mine, theirs in (
            ("tools", self.tools, other.tools),
            ("files", self.files, other.files),
            ("network", self.network, other.network),
            ("secrets", self.secrets, other.secrets),
            ("skills", self.skills, other.skills),
            (
                "memory",
                frozenset(item.value for item in self.memory),
                frozenset(item.value for item in other.memory),
            ),
        ):
            extra = tuple(sorted(theirs - mine))
            if extra:
                found[face] = extra
        return found


    def document(self) -> dict[str, Any]:
        """The scope as it is written onto a child Run.

        Sorted lists rather than sets, because this is stored and read back:
        two equal scopes must serialize identically or a snapshot comparison
        turns into a set comparison somebody forgot to write.
        """
        return {
            "tools": sorted(self.tools),
            "files": sorted(self.files),
            "network": sorted(self.network),
            "secrets": sorted(self.secrets),
            "skills": sorted(self.skills),
            "memory": sorted(item.value for item in self.memory),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "DelegationScope":
        """Read back a stored scope.

        A face this version does not recognize is dropped and a face that is
        missing is **empty**, which is the same answer `intersect` gives for no
        arguments and for the same reason: a scope read by a newer version of
        this platform must not come back wider than it was written.
        """
        memory: list[MemoryPermission] = []
        for name in document.get("memory", ()):
            try:
                memory.append(MemoryPermission(name))
            except ValueError:
                continue
        return cls.of(
            tools=document.get("tools", ()),
            files=document.get("files", ()),
            network=document.get("network", ()),
            secrets=document.get("secrets", ()),
            skills=document.get("skills", ()),
            memory=memory,
        )
def intersect(*scopes: DelegationScope) -> DelegationScope:
    """What survives every scope in the chain.

    **No arguments is empty, not everything.** A vacuous "all" would turn a
    missing layer into a child holding more than anybody granted, and a missing
    layer is exactly what a bug looks like here.

    There is deliberately no union, no `widen`, and no way to add a name that
    was not already in every argument. That is the whole control: not that
    widening is forbidden, but that it cannot be written.
    """
    if not scopes:
        return DelegationScope()
    first, *rest = scopes
    tools, files = first.tools, first.files
    network, secrets = first.network, first.secrets
    skills, memory = first.skills, first.memory
    for scope in rest:
        tools &= scope.tools
        files &= scope.files
        network &= scope.network
        secrets &= scope.secrets
        skills &= scope.skills
        memory &= scope.memory
    return DelegationScope(
        tools=tools,
        files=files,
        network=network,
        secrets=secrets,
        skills=skills,
        memory=memory,
    )


def scope_of_spec(spec: AgentSpec) -> DelegationScope:
    """What an Agent itself holds, in the faces a spec actually binds.

    Four of the six, and the two that are missing are missing on purpose.
    `files` are Artifact ids and `secrets` are references a tool resolves at
    call time — neither is bound on a spec, so there is nothing here to compare
    a delegation's against. They are narrowed by the delegation and **checked
    where they are used**: a child naming an artifact its parent cannot read is
    refused when it reads.

    Saying that rather than silently comparing against an empty set: the latter
    would refuse every delegation that passes a file, which is the ordinary case
    §13's eighth clause exists for.
    """
    return DelegationScope.of(
        tools=spec.tools,
        network=spec.network.allow if spec.network is not None else (),
        skills=tuple(str(binding.skill_version_id) for binding in spec.skills),
        memory=(
            (MemoryPermission.READ_PRIVATE, MemoryPermission.PROPOSE_PRIVATE)
            if "memory.remember" in spec.tools
            else (MemoryPermission.READ_PRIVATE,)
        ),
    )


def asked_by(binding: ChildBinding) -> DelegationScope:
    """What a binding wrote down, before any intersection."""
    return DelegationScope.of(
        tools=binding.tools,
        files=binding.files,
        network=binding.network,
        secrets=binding.secrets,
        skills=binding.skills,
        memory=binding.memory,
    )


def granted(parent: AgentSpec, binding: ChildBinding) -> DelegationScope:
    """What a child actually gets: the parent's own scope met with the binding.

    The one function a creation path calls, so there is no second place where
    somebody could assemble a scope out of a binding alone. Publishing already
    refused a binding wider than its parent — this is the same answer computed
    again at the moment it is written onto a Run, because between publishing and
    running is exactly where a scope could otherwise drift.
    """
    return intersect(scope_of_spec(parent), asked_by(binding))
