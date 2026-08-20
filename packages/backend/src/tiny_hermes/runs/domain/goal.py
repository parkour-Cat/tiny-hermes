"""Whether the goal was met, decided by the platform rather than the model.

Product design §12.1 gives the judge three answers — `done`, `continue` and
`wait` — and §531 makes the model's own claim a proposal: 模型的判断只是建议，
服务端必须验证状态、预算和权限. Through 0.1 there was no judge, and
`slice_policy` ended a Run the moment a provider set `StopReason.COMPLETED`.

Two answers here are not judgements and exist so that nothing has to be
smuggled around this function: `failed` passes a broken round through
untouched, and `undecidable` says the platform could not observe the evidence
it needed.

The judge answers *was the goal met* and nothing else. What state the Run
enters — and the fact that a cancellation, a pause request or an exhausted
budget outranks every verdict below — stays in `decide_after_round`, which has
held that order since 0.1. Two places deciding Run state is one too many.
"""

from dataclasses import dataclass
from enum import StrEnum

from tiny_hermes.runs.ports.model import StopReason


class GoalOutcome(StrEnum):
    DONE = "done"
    CONTINUE = "continue"
    WAIT = "wait"
    #: The round itself broke. Not a judgement; the evidence is not consulted.
    FAILED = "failed"
    #: The platform could not run the declared checks, so it will neither
    #: accept nor reject the claim.
    UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class GoalProposal:
    """What the round proposed, in the platform's own terms."""

    stop_reason: StopReason
    #: Set when the round asked to be woken later rather than to keep working.
    wait_seconds: int | None = None


@dataclass(frozen=True)
class CompletionCheck:
    """One declared condition, and whether the platform found it met.

    ``name`` is what the instruction will say out loud, so it is the condition
    as declared — a command, or a path under ``/workspace/data`` — not an
    internal identifier.
    """

    name: str
    met: bool


@dataclass(frozen=True)
class GoalEvidence:
    """What the platform observed about the proposal.

    ``declared`` is false for every Agent published before this slice, and for
    those the model's claim is still the whole of the answer. That is not a
    concession: an Agent that declares nothing has given the platform nothing
    to check, and inventing a check would change what published, immutable
    versions do.

    ``observable`` is false when the checks could not be run at all — no
    sandbox, a refusing controller — as opposed to running and failing.
    """

    declared: bool
    checks: tuple[CompletionCheck, ...] = ()
    observable: bool = True


@dataclass(frozen=True)
class GoalVerdict:
    outcome: GoalOutcome
    #: The declared conditions this round did not meet, in declaration order.
    unmet: tuple[str, ...] = ()
    #: What the next round is told, present only when ``outcome`` is
    #: ``continue`` and something checkable came back unmet.
    instruction: str | None = None
    wait_seconds: int | None = None


def _instruction_for(unmet: tuple[str, ...]) -> str:
    """Name what is still unmet, and nothing else.

    The task is already in the conversation. Restating it spends context on
    something the model can read, while the one thing it cannot know is why the
    platform disagreed with its claim.
    """
    if len(unmet) == 1:
        return f"The task is not finished: {unmet[0]} did not pass. Continue working on it."
    listed = "; ".join(unmet)
    return f"The task is not finished: {listed} did not pass. Continue working on them."


def judge(proposal: GoalProposal, evidence: GoalEvidence) -> GoalVerdict:
    """Decide whether this round met the goal.

    Order is the product rule. A broken round is reported as broken before
    anything else is considered; a completion claim is then verified; only a
    round that neither finished nor broke may ask to wait.
    """
    if proposal.stop_reason is StopReason.FAILED:
        return GoalVerdict(GoalOutcome.FAILED)

    if proposal.stop_reason is StopReason.COMPLETED:
        if not evidence.declared:
            return GoalVerdict(GoalOutcome.DONE)
        if not evidence.observable:
            return GoalVerdict(GoalOutcome.UNDECIDABLE)
        unmet = tuple(check.name for check in evidence.checks if not check.met)
        if unmet:
            return GoalVerdict(
                GoalOutcome.CONTINUE, unmet=unmet, instruction=_instruction_for(unmet)
            )
        if not evidence.checks:
            # A declared goal that nothing has verified is not a met goal. The
            # claim arrived before the platform had anything to weigh it
            # against, so the round goes back for the checks to be run.
            return GoalVerdict(GoalOutcome.CONTINUE)
        return GoalVerdict(GoalOutcome.DONE)

    if proposal.wait_seconds is not None and proposal.wait_seconds > 0:
        return GoalVerdict(GoalOutcome.WAIT, wait_seconds=proposal.wait_seconds)

    return GoalVerdict(GoalOutcome.CONTINUE)
