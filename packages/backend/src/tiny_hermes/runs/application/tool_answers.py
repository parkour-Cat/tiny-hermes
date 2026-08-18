"""The answers the platform gives itself, for the tools no sandbox runs.

`platform.wait` and `skill.load` are bound like any other tool and appear in
the same schema list, but what they do happens on the Run rather than in a
container — one moves the Run into `waiting_external`, the other brings a
workspace's document into the conversation. Neither ever becomes a
`SandboxCommand`, so neither passes through `authorize`, and the binding check
each one needs is written here instead.

That repetition is the point rather than an oversight. §10.2's two steps are
different things: the schemas are what the model is *told about*, and this
module is what actually runs. A model that asks for something it was not bound
is refused here whether or not it was ever shown the schema.

Every path returns a result. A model left without an answer to a call it made
will retry it or invent what it returned.
"""

from uuid import UUID

from tiny_hermes.runs.domain.models import (
    RunEventType,
    ToolCallBlock,
    ToolResultBlock,
)
from tiny_hermes.runs.ports.proposals import SkillProposals
from tiny_hermes.runs.ports.skills import SkillLibrary
from tiny_hermes.runs.ports.store import ExecutionContext, ReservedEvent
from tiny_hermes.tools.domain.registry import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_LOADS,
    RefusalReason,
    ToolRefused,
    skill_load_of,
    skill_propose_of,
    wait_seconds_of,
)


def refusal(call_id: str, reason: RefusalReason, detail: str = "") -> ToolResultBlock:
    """A named refusal, in the one shape every refused call comes back in."""
    return ToolResultBlock(
        call_id=call_id,
        output=f"refused: {reason.value}" + (f" ({detail})" if detail else ""),
        exit_code=126,
        failed=True,
    )


def text_refusal(call_id: str, why: str) -> ToolResultBlock:
    """A refusal whose reason is a fact about this Run rather than a category.

    A ceiling reached and a file that is not there are not `invalid_arguments`
    — the call was well formed and the model could not have known. It gets a
    sentence it can act on instead of a code it would have to guess at.
    """
    return ToolResultBlock(
        call_id=call_id, output=f"refused: {why}", exit_code=126, failed=True
    )


def answer_platform_tool(
    call: ToolCallBlock, bound: tuple[str, ...]
) -> tuple[ToolResultBlock, int | None]:
    """Answer `platform.wait`, and say how long the Run is to be left alone."""
    if call.name not in bound:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED), None
    try:
        seconds = wait_seconds_of(call)
    except ToolRefused as refused:
        return refusal(refused.call_id, refused.reason), None
    # Written in the past tense on purpose: by the time the model reads this
    # turn, the wait is over and the Run has been woken.
    return (
        ToolResultBlock(
            call_id=call.call_id,
            output=f"waited {seconds} seconds",
            exit_code=0,
            failed=False,
        ),
        seconds,
    )


async def answer_skill_load(
    library: SkillLibrary | None,
    context: ExecutionContext,
    call: ToolCallBlock,
    loaded: list[UUID],
) -> tuple[ToolResultBlock, ReservedEvent | None]:
    """Bring one skill file into the conversation, or say why not.

    This is §10.2's second check in the shape §10.1 gives it. The model names a
    skill; what is authorized is the set of version ids *this Run* was
    published with. A name outside that set is refused exactly as an unbound
    tool is — the summaries the model was handed are not a control, the
    bindings are.

    An event comes back with the result only when text was actually read. A
    refusal loaded nothing, so counting it against the Run's eight would spend
    the ceiling on the model's typing mistakes.
    """
    if "skill.load" not in context.spec.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED), None
    try:
        asked = skill_load_of(call)
    except ToolRefused as refused:
        return refusal(call.call_id, refused.reason, refused.detail), None
    if len(loaded) >= MAX_SKILL_LOADS:
        return (
            text_refusal(
                call.call_id,
                f"this Run has already loaded skill text {MAX_SKILL_LOADS} "
                f"times, which is the limit",
            ),
            None,
        )
    bound = next((skill for skill in context.skills if skill.name == asked.skill), None)
    if bound is None:
        # The same refusal an unbound tool gets, and for the same reason: what
        # the model may reach is what the Version bound.
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED, asked.skill), None
    if library is None:
        return text_refusal(call.call_id, "no skill catalog is configured here"), None
    text = await library.read_file(bound.skill_version_id, asked.path)
    if text is None:
        return text_refusal(call.call_id, f"{asked.skill} has no file at {asked.path}"), None
    size = len(text.encode())
    if size > MAX_SKILL_FILE_BYTES:
        # Refused with its size rather than truncated, the same rule the
        # planner follows: a model handed half a document cannot tell that it
        # is holding half, and will act on the half it got.
        return (
            text_refusal(
                call.call_id,
                f"{asked.path} is {size} bytes, over the "
                f"{MAX_SKILL_FILE_BYTES} byte limit for one load",
            ),
            None,
        )
    return (
        ToolResultBlock(call_id=call.call_id, output=text, exit_code=0, failed=False),
        ReservedEvent(
            event_type=RunEventType.SKILL_LOADED,
            payload={
                "skill": bound.name,
                "path": asked.path,
                "skill_version_id": str(bound.skill_version_id),
                "bytes": size,
            },
        ),
    )


async def answer_skill_propose(
    proposals: SkillProposals | None,
    context: ExecutionContext,
    call: ToolCallBlock,
) -> tuple[ToolResultBlock, ReservedEvent | None]:
    """Open a proposal, and tell the model plainly that nothing changed yet.

    §15.3's first step, and the roadmap's "the model's judgment is only a
    suggestion" in the one place where a model could mistake it for more. The
    result says what was written — a pending proposal — so a model cannot
    reasonably continue as though the skill it proposed is now in force.

    Patching is scoped the way loading is: the skill named must be one this
    Run's Version bound, and the base of the diff is the exact version this Run
    was given. A skill the Agent never read is not one it is in a position to
    rewrite. Naming nothing proposes a new skill, which needs no binding
    because there is nothing yet to be bound to.
    """
    if "skill.propose" not in context.spec.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED), None
    try:
        asked = skill_propose_of(call)
    except ToolRefused as refused:
        return refusal(call.call_id, refused.reason, refused.detail), None
    base: UUID | None = None
    if asked.skill is not None:
        bound = next(
            (skill for skill in context.skills if skill.name == asked.skill), None
        )
        if bound is None:
            return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED, asked.skill), None
        base = bound.skill_version_id
    if proposals is None:
        return text_refusal(call.call_id, "no skill catalog is configured here"), None
    outcome = await proposals.propose(
        run_id=context.run_id, skill_version_id=base, files=asked.files
    )
    if outcome.proposal_id is None:
        return text_refusal(call.call_id, outcome.refusal or "the proposal was refused"), None
    return (
        ToolResultBlock(
            call_id=call.call_id,
            output=(
                f"Opened proposal {outcome.proposal_id} for a person to review. "
                f"Nothing has changed: no version exists until someone approves "
                f"it, and no Agent uses a new version until it is republished."
            ),
            exit_code=0,
            failed=False,
        ),
        ReservedEvent(
            event_type=RunEventType.SKILL_PROPOSED,
            payload={
                "proposal_id": str(outcome.proposal_id),
                "skill": asked.skill or "",
                "files": len(asked.files),
            },
        ),
    )
