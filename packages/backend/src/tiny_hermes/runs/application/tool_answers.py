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

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from tiny_hermes.agents.domain.models import WritePolicy
from tiny_hermes.memory.domain.policy import CandidateOutcome
from tiny_hermes.memory.domain.search import SearchRefused, request_for
from tiny_hermes.runs.domain.approval import ApprovalType, normalize_call
from tiny_hermes.runs.domain.models import (
    RunEventType,
    ToolCallBlock,
    ToolResultBlock,
    WaitPolicy,
)
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalGate
from tiny_hermes.runs.ports.artifacts import ArtifactReads
from tiny_hermes.runs.ports.children import (
    ChildRuns,
    DelegationRequest,
    DelegationWait,
)
from tiny_hermes.runs.ports.http_calls import EgressClaim, HttpToolSender
from tiny_hermes.runs.ports.mcp import BoundMcpTool, McpGateway
from tiny_hermes.runs.ports.memories import MemoryCandidates
from tiny_hermes.runs.ports.proposals import SkillProposals
from tiny_hermes.runs.ports.searches import SessionSearches
from tiny_hermes.runs.ports.skills import SkillLibrary
from tiny_hermes.runs.ports.store import ExecutionContext, ReservedEvent
from tiny_hermes.tools.domain.http_calls import (
    BoundOperation,
    HttpCallRefused,
    HttpRequestPlan,
    http_call_of,
)
from tiny_hermes.tools.domain.mcp import call_name as mcp_call_name
from tiny_hermes.tools.domain.registry import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_LOADS,
    MAX_WAIT_SECONDS,
    RefusalReason,
    ToolRefused,
    artifact_read_of,
    delegation_of,
    memory_body_of,
    session_search_of,
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
    if "skill.load" not in context.tools:
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
    bound = next(
        (skill for skill in context.granted_skills if skill.name == asked.skill), None
    )
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
    if "skill.propose" not in context.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED), None
    try:
        asked = skill_propose_of(call)
    except ToolRefused as refused:
        return refusal(call.call_id, refused.reason, refused.detail), None
    base: UUID | None = None
    if asked.skill is not None:
        bound = next(
            (skill for skill in context.granted_skills if skill.name == asked.skill),
            None,
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


async def answer_memory_remember(
    candidates: MemoryCandidates | None,
    context: ExecutionContext,
    call: ToolCallBlock,
) -> tuple[ToolResultBlock, ReservedEvent | None]:
    """Offer one candidate, and tell the model the truth about what happened.

    §14.1's write path, and the same care `skill.propose` takes for the same
    reason: what the model just did is a *proposal*, and a result that read like
    a confirmation would leave it carrying on as though the thing were
    remembered. The three outcomes are three different sentences — refused,
    waiting for a person, written — because a model told only "done" cannot
    tell them apart, and only one of them is done.

    The scope is never an argument. A Run proposes a memory about the person it
    is working with and about nobody else; the subject is the one the catalog
    reads off this Run, so there is nothing here a model could point elsewhere.
    """
    if "memory.remember" not in context.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED), None
    try:
        body = memory_body_of(call)
    except ToolRefused as refused_call:
        return refusal(call.call_id, refused_call.reason, refused_call.detail), None
    if candidates is None:
        return text_refusal(call.call_id, "no memory store is configured here"), None
    result = await candidates.propose(run_id=context.run_id, body=body)
    if result.outcome is CandidateOutcome.REFUSED:
        # Not a failed call — the workspace decided this, and the model could
        # not have known. A sentence it can report, not an error code.
        return text_refusal(
            call.call_id, result.detail or "this workspace declined to record it"
        ), None
    if result.outcome is CandidateOutcome.WRITTEN:
        return (
            ToolResultBlock(
                call_id=call.call_id,
                output=(
                    "Recorded for future conversations. It does not affect this "
                    "Run."
                ),
                exit_code=0,
                failed=False,
            ),
            ReservedEvent(
                event_type=RunEventType.MEMORY_WRITTEN,
                payload={"memory_id": str(result.memory_id)},
            ),
        )
    return (
        ToolResultBlock(
            call_id=call.call_id,
            output=(
                "Proposed for a person to review. Nothing has changed and this "
                "Run will not use it; it is remembered only if someone approves "
                "it."
            ),
            exit_code=0,
            failed=False,
        ),
        ReservedEvent(
            event_type=RunEventType.MEMORY_PROPOSED,
            payload={"memory_id": str(result.memory_id)},
        ),
    )


@dataclass(frozen=True)
class DelegationOutcome:
    """What the round has to carry away from one `agent.delegate` call."""

    result: ToolResultBlock
    event: ReservedEvent | None = None
    #: Set only when children were created. The round that made them does not
    #: go on to be judged — it hands the Run over to a wait.
    wait: DelegationWait | None = None


async def answer_agent_delegate(
    children: ChildRuns | None,
    context: ExecutionContext,
    call: ToolCallBlock,
    now: datetime | None = None,
) -> DelegationOutcome:
    """Hand work to other Agents, or say plainly why nothing was handed over.

    §13. The result is written for a model that has to decide what to do next,
    which is why a refusal is a sentence rather than a code: an Agent told
    "not_authorized" will try a different alias, and one told it may not
    delegate at all can get on with the work itself.

    Almost nothing is decided here. Whether this Run may delegate, whether the
    aliases are bound, how many may run at once and what each child ends up
    permitted to do are all settled where the children are created — including
    §13's third clause, which is decided from the parent's own `depth` row.
    The check this function does make is the binding check §10.2 requires of
    every platform tool: a model that asks for a tool its Version did not bind
    is refused whether or not it was shown the schema.

    **How long the parent waits is not the model's to choose.** The deadline
    is whatever is left of this Run's own elapsed budget, capped: waiting past
    the moment the parent itself runs out is waiting for an answer it could not
    act on, and a `seconds` argument would be a way to ask for exactly that.
    """
    if "agent.delegate" not in context.tools:
        return DelegationOutcome(refusal(call.call_id, RefusalReason.NOT_AUTHORIZED))
    try:
        asked, wait = delegation_of(call)
    except ToolRefused as refused_call:
        return DelegationOutcome(
            refusal(call.call_id, refused_call.reason, refused_call.detail)
        )
    if children is None:
        return DelegationOutcome(
            text_refusal(call.call_id, "no delegation is configured here")
        )
    result = await children.delegate(
        parent_run_id=context.run_id,
        requests=tuple(
            DelegationRequest(
                alias=alias, instruction=instruction, artifacts=artifacts
            )
            for alias, instruction, artifacts in asked
        ),
    )
    if result.refused:
        # Not a failed call. Every one of these is something the platform
        # decided and the model could not have known, so it comes back as
        # something it can read and act on.
        return DelegationOutcome(
            text_refusal(call.call_id, result.refusal or "nothing was delegated")
        )
    policy = WaitPolicy(wait)
    listed = ", ".join(f"{child.alias} ({child.run_id})" for child in result.children)
    settle = (
        "You will be woken when all of them have finished."
        if policy is WaitPolicy.ALL
        else "You will be woken as soon as one succeeds, and the rest cancelled."
    )
    return DelegationOutcome(
        result=ToolResultBlock(
            call_id=call.call_id,
            output=(
                f"Started {len(result.children)}: {listed}. They spend the same "
                f"budget you do. {settle} Stop working now — anything you do "
                f"after this is discarded."
            ),
            exit_code=0,
            failed=False,
        ),
        event=ReservedEvent(
            event_type=RunEventType.RUN_DELEGATED,
            payload={
                "wait": policy.value,
                "children": [
                    {
                        "run_id": str(child.run_id),
                        "session_id": str(child.session_id),
                        "alias": child.alias,
                    }
                    for child in result.children
                ],
            },
        ),
        wait=DelegationWait(
            child_run_ids=tuple(child.run_id for child in result.children),
            policy=policy,
            seconds=_child_wait_seconds(context, now),
        ),
    )


def _child_wait_seconds(context: ExecutionContext, now: datetime | None) -> int:
    """How long the parent hangs on, from its own budget rather than the model.

    Whatever is left of this Run's elapsed deadline, floored at a minute so a
    parent that delegates near the end still gets a wait that can be observed,
    and capped at the platform's own ceiling. A Run waiting past the point it
    could act on an answer is a Session head nobody can use.
    """
    at = now or datetime.now(UTC)
    remaining = int((context.budget.elapsed_deadline_at - at).total_seconds())
    return max(60, min(MAX_WAIT_SECONDS, remaining))


async def answer_artifact_read(
    reads: ArtifactReads | None,
    context: ExecutionContext,
    call: ToolCallBlock,
) -> ToolResultBlock:
    """Open a file this Run was passed, or say why it cannot be.

    §13's eighth clause from the reading end. What makes a file reachable is a
    grant, and the grant is checked against **this Run** — not its Agent and
    not its Session — so a later Run of the same Agent cannot open what nobody
    passed to this piece of work.

    The refusals are sentences a model can act on, and one of them is
    deliberately vague: "does not exist" and "not yours" come back identically,
    because telling them apart would let an Agent map which ids are real by
    reading the refusals it gets.
    """
    if "artifact.read" not in context.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED)
    try:
        wanted = artifact_read_of(call)
    except ToolRefused as refused_call:
        return refusal(call.call_id, refused_call.reason, refused_call.detail)
    if reads is None:
        return text_refusal(call.call_id, "no file store is configured here")
    found = await reads.read(run_id=context.run_id, artifact_id=wanted)
    if found.text is None:
        return text_refusal(call.call_id, found.detail or "it could not be read")
    return ToolResultBlock(
        call_id=call.call_id,
        output=found.text,
        exit_code=0,
        failed=False,
    )


async def answer_session_search(
    searches: SessionSearches | None,
    context: ExecutionContext,
    call: ToolCallBlock,
) -> ToolResultBlock:
    """Look through this subject's past conversations, and return snippets.

    §14.3's whole point is that this is **on demand and partial**: a search
    that returned conversations would put the history back in the context by
    another name. So each hit is a bounded snippet, a shortened one says so,
    and the page is small.

    Whose sessions are searched is never an argument. It is this Run's own
    subject, read where the search runs, so there is nothing here a model could
    point at somebody else.
    """
    if "session.search" not in context.tools:
        return refusal(call.call_id, RefusalReason.NOT_AUTHORIZED)
    try:
        query, limit = session_search_of(call)
    except ToolRefused as refused_call:
        return refusal(call.call_id, refused_call.reason, refused_call.detail)
    if searches is None:
        return text_refusal(call.call_id, "no session search is configured here")
    try:
        asked = request_for(query, limit)
    except SearchRefused as refused_search:
        return text_refusal(call.call_id, str(refused_search))
    hits = await searches.for_run(run_id=context.run_id, request=asked)
    if not hits:
        return ToolResultBlock(
            call_id=call.call_id,
            output="No past message matched that.",
            exit_code=0,
            failed=False,
        )
    lines = [
        f"[{hit.sequence}] {hit.role}: {hit.snippet}"
        + (" …(shortened)" if hit.shortened else "")
        for hit in hits
    ]
    return ToolResultBlock(
        call_id=call.call_id,
        output="\n".join(lines),
        exit_code=0,
        failed=False,
    )


@dataclass(frozen=True)
class HttpCallOutcome:
    """What a round does with one HTTP tool call.

    Three shapes rather than one, because a call that is waiting for a person
    is not a call that failed. `result` is `None` exactly when the Run must
    stop: nothing is appended to the transcript, so the resumed Run asks the
    model again and the gate answers `approved` the second time.

    Appending nothing is the decision worth stating. A tool result saying "you
    are waiting" would be in the conversation forever, and the model would have
    to be trusted to reissue the same call after being told it had already made
    it.
    """

    result: ToolResultBlock | None
    event: ReservedEvent | None = None
    #: Set when the Run must stop, carrying which of the three not-approved
    #: answers came back.
    approval: ApprovalCheck | None = None


async def answer_http_call(
    sender: HttpToolSender | None,
    context: ExecutionContext,
    call: ToolCallBlock,
    claim: EgressClaim,
    gate: ApprovalGate | None = None,
) -> HttpCallOutcome:
    """Call somebody else's API, wait for a person, or say why not.

    Answered on the platform's side for the same reason `skill.load` is: what
    this asks for happens outside the container, and the credential must never
    be inside one.

    §16.3 in three lines. A read runs. A write runs only under the policy its
    Version chose at publish: `disabled` refuses, `preauthorized` proceeds
    because a workspace administrator already approved this narrow scope, and
    `governance` asks and the Run waits.

    The approval is bound to the **composed** call: a person is shown the URL
    that would actually be requested, and the hash covers it. That is why the
    arguments are validated first — an approval document carrying a parameter
    the operation never declared would describe a request this platform would
    refuse to make anyway.
    """
    bound = list(context.granted_operations)
    entry = next((item for item in bound if item.call_name == call.name), None)
    if entry is None:
        # The same refusal `http_call_of` would give, made here because the
        # binding has to be known before anything else is decided.
        return HttpCallOutcome(
            refusal(call.call_id, RefusalReason.NOT_AUTHORIZED, call.name)
        )
    try:
        plan = http_call_of(call, bound)
    except HttpCallRefused as refused:
        reason = (
            RefusalReason.NOT_AUTHORIZED
            if refused.reason == "tool_not_authorized"
            else RefusalReason.INVALID_ARGUMENTS
        )
        return HttpCallOutcome(refusal(call.call_id, reason, refused.detail))

    if not plan.read_only:
        stopped = await _cleared_to_write(entry, plan, call, gate, context)
        if stopped is not None:
            return stopped

    if sender is None:
        return HttpCallOutcome(
            text_refusal(call.call_id, "no outbound face is configured here")
        )
    answer = await sender.send(plan, entry.credential_ref, claim)
    if answer.refusal is not None:
        return HttpCallOutcome(
            text_refusal(call.call_id, answer.refusal),
            _refused_event(entry, plan, answer.refusal),
        )
    return HttpCallOutcome(
        ToolResultBlock(
            call_id=call.call_id,
            # The status travels with the body because a model given only a
            # body cannot tell an answer from an error document.
            output=f"HTTP {answer.status_code}\n{answer.body}",
            exit_code=0 if not answer.failed else 1,
            failed=answer.failed,
        )
    )


async def _cleared_to_write(
    entry: BoundOperation,
    plan: HttpRequestPlan,
    call: ToolCallBlock,
    gate: ApprovalGate | None,
    context: ExecutionContext,
) -> HttpCallOutcome | None:
    """`None` when the write may proceed; an outcome when it may not.

    The policy comes from the binding rather than from anything the model or
    the far end says, and a binding with no policy is read as `disabled`.
    Publishing refuses that combination, so reaching it means a version
    published before the check existed — and refusing is the safe reading of
    silence.
    """
    policy = _write_policy(entry, context)
    if policy is WritePolicy.PREAUTHORIZED:
        # A workspace administrator approved this narrow scope when the version
        # was published. Nothing to ask at runtime: that is what the choice
        # means, and the version records who made it.
        return None
    if policy is not WritePolicy.GOVERNANCE:
        return HttpCallOutcome(
            text_refusal(
                call.call_id,
                "write_disabled: this Agent was published with writes to this "
                "tool turned off. Do not retry it; report what you were unable "
                "to do.",
            ),
            _refused_event(entry, plan, "write_disabled"),
        )
    if gate is None:
        return HttpCallOutcome(
            text_refusal(call.call_id, "no approval gate is configured here"),
            _refused_event(entry, plan, "approval_unavailable"),
        )
    permission = f"http.{entry.tool_name}.write"
    normalized = normalize_call(
        call.name,
        call.arguments,
        target=plan.url,
        required_permission=permission,
    )
    checked = await gate.check(
        run_id=context.run_id,
        approval_type=ApprovalType.GOVERNANCE_APPROVAL,
        tool=call.name,
        call_id=call.call_id,
        call=normalized,
        required_permission=permission,
    )
    if checked.proceeds:
        return None
    # Nothing appended: the Run stops here and asks the model again when it
    # resumes. See `HttpCallOutcome`.
    return HttpCallOutcome(None, approval=checked)


def _write_policy(
    entry: BoundOperation, context: ExecutionContext
) -> WritePolicy | None:
    binding = next(
        (
            item
            for item in context.spec.http_tools
            if item.http_tool_version_id == entry.version_id
        ),
        None,
    )
    return None if binding is None else binding.write_policy


def _refused_event(
    entry: BoundOperation, plan: HttpRequestPlan, reason: str
) -> ReservedEvent:
    return ReservedEvent(
        event_type=RunEventType.HTTP_CALL_REFUSED,
        payload={
            "tool": entry.tool_name,
            "operation": entry.operation.operation_id,
            "method": plan.method,
            "reason": reason,
        },
    )


async def answer_mcp_call(
    gateway: McpGateway | None,
    context: ExecutionContext,
    call: ToolCallBlock,
    revalidated: tuple[BoundMcpTool, ...],
    claim: EgressClaim,
    approvals: ApprovalGate | None = None,
) -> HttpCallOutcome:
    """Call a bound MCP tool, wait for a person, or say why not.

    §16.2's second step, and the important half: the call is authorized against
    the **revalidated subset**, never against the name the model typed. A tool
    the Version did not bind is not in that subset no matter what the server
    advertises, and a tool the server dropped is not in it either.

    §16.3's approval applies exactly as it does to an HTTP write, with one
    difference that is a fact about MCP rather than a choice: a server does not
    say which of its tools change something — there is no `GET` to read — so
    the platform cannot tell, and every MCP call is treated as one that might.
    A binding therefore chooses a policy for all of its tools or for none, and
    `disabled` is what silence means.
    """
    entry = next(
        (
            item
            for item in revalidated
            if mcp_call_name(item.server_name, item.tool.name) == call.name
        ),
        None,
    )
    if entry is None:
        # The same refusal an unbound HTTP operation gets. What the model may
        # reach is what the Version bound and the server still offers.
        return HttpCallOutcome(
            refusal(call.call_id, RefusalReason.NOT_AUTHORIZED, call.name)
        )
    policy = _mcp_policy(entry, context)
    if policy is WritePolicy.PREAUTHORIZED:
        pass
    elif policy is not WritePolicy.GOVERNANCE:
        return HttpCallOutcome(
            text_refusal(
                call.call_id,
                "write_disabled: this Agent was published with calls to this "
                "server turned off. Do not retry it; report what you were "
                "unable to do.",
            ),
            _mcp_event(entry, "write_disabled"),
        )
    else:
        if approvals is None:
            return HttpCallOutcome(
                text_refusal(call.call_id, "no approval gate is configured here"),
                _mcp_event(entry, "approval_unavailable"),
            )
        permission = f"mcp.{entry.server_name}.call"
        normalized = normalize_call(
            call.name,
            call.arguments,
            target=f"mcp://{entry.server_name}/{entry.tool.name}",
            required_permission=permission,
        )
        checked = await approvals.check(
            run_id=context.run_id,
            approval_type=ApprovalType.GOVERNANCE_APPROVAL,
            tool=call.name,
            call_id=call.call_id,
            call=normalized,
            required_permission=permission,
        )
        if not checked.proceeds:
            # Nothing appended: the Run stops here and asks the model again
            # when it resumes. See `HttpCallOutcome`.
            return HttpCallOutcome(None, approval=checked)

    if gateway is None:
        return HttpCallOutcome(
            text_refusal(call.call_id, "no MCP gateway is configured here")
        )
    answer = await gateway.call(entry, dict(call.arguments), claim)
    if answer.failed:
        return HttpCallOutcome(
            text_refusal(call.call_id, f"{answer.refusal}: {answer.content}".strip(": ")),
            _mcp_event(entry, answer.refusal or "refused"),
        )
    return HttpCallOutcome(
        ToolResultBlock(
            call_id=call.call_id,
            # The server's own words, unread. A tool result is reference
            # material the model weighs, not instructions this platform follows.
            output=answer.content,
            exit_code=0,
            failed=False,
        )
    )


def _mcp_policy(entry: BoundMcpTool, context: ExecutionContext) -> WritePolicy | None:
    binding = next(
        (
            item
            for item in context.spec.mcp_tools
            if item.mcp_server_version_id == entry.version_id
        ),
        None,
    )
    return None if binding is None else binding.write_policy


def _mcp_event(entry: BoundMcpTool, reason: str) -> ReservedEvent:
    return ReservedEvent(
        event_type=RunEventType.HTTP_CALL_REFUSED,
        payload={
            "tool": entry.server_name,
            "operation": entry.tool.name,
            "method": "mcp",
            "reason": reason,
        },
    )
