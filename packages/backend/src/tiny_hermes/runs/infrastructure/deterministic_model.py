import asyncio
from typing import Any, cast

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.runs.domain.models import TextBlock, ToolCallBlock, ToolResultBlock
from tiny_hermes.runs.ports.model import (
    ModelRequest,
    ModelResponse,
    StopReason,
    UsageQuality,
)
from tiny_hermes.tools.domain.http_calls import HTTP_PREFIX
from tiny_hermes.tools.domain.mcp import MCP_PREFIX

MAX_DELAY_MS = 5_000
TOKENS_PER_ROUND = 32
#: Long enough that a test can see the Run sitting in `waiting_external`, short
#: enough that a real deployment's drill is not left there. Tests that want the
#: deadline reached move it in the row rather than sleeping through it.
WAIT_SECONDS = 60


class DeterministicModelProvider:
    """The only phase-2B model provider.

    It is not a test double. A published Agent Version selects one of its
    scenarios through a validated model policy, and the provider answers the
    same way every time so a Run's behavior is reproducible. It performs no
    network call, so no outbound policy applies to it yet.
    """

    def __init__(self, delay_ms: int = 50) -> None:
        if not 0 <= delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"delay must be between 0 and {MAX_DELAY_MS} milliseconds")
        self._delay_seconds = delay_ms / 1000

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        policy = request.policy
        if not isinstance(policy, DeterministicModelPolicy):
            # Reaching here is a routing mistake, not a user's. It fails the
            # round rather than raising, because a Worker that crashes on a
            # misrouted Run loses the whole slice instead of one Run.
            return ModelResponse(
                stop_reason=StopReason.FAILED,
                text="",
                usage_quality=UsageQuality.UNAVAILABLE,
                failure="policy_mismatch",
            )
        scenario = policy.scenario
        if scenario == "shell_once":
            results = tuple(
                block
                for message in request.messages
                for block in message.blocks
                if isinstance(block, ToolResultBlock) and block.call_id == "drill-shell-1"
            )
            if not results:
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Running the deterministic sandbox drill command.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="drill-shell-1",
                            name="shell.exec",
                            arguments={
                                "command": (
                                    "printf 'drill-started\\n'; sleep 20; "
                                    "printf 'drill-finished\\n'"
                                ),
                                "timeout_seconds": 60,
                            },
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            result = results[-1]
            if result.failed or "drill-finished" not in result.output:
                return ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                    failure="deterministic_shell_output_missing",
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text="The model observed real command output: drill-finished.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario == "shell_from_input":
            # The workspace drill's whole vocabulary: the Run's input *is* the
            # command, and this scenario is the assertion. A command that
            # failed, exited non-zero, or was rolled back by the platform is
            # reported as exactly that, so a drill reads verdicts from Run
            # status instead of scraping transcripts.
            #
            # Only results *after the last user message* count: in a shared
            # Session the transcript already carries earlier Runs' results
            # under the same call id, and believing one of those would skip
            # this Run's command entirely.
            results = tuple(
                block
                for message in request.messages[_last_user_index(request) + 1 :]
                for block in message.blocks
                if isinstance(block, ToolResultBlock)
                and block.call_id == "input-shell-1"
            )
            if not results:
                command = _last_user_text(request)
                if not command:
                    return ModelResponse(
                        stop_reason=StopReason.FAILED,
                        text="",
                        failure="deterministic_no_input_command",
                    )
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Running the drill's command from the Run input.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="input-shell-1",
                            name="shell.exec",
                            arguments={"command": command, "timeout_seconds": 120},
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            outcome = results[-1]
            if "rolled back:" in outcome.output:
                return ModelResponse(
                    stop_reason=StopReason.COMPLETED,
                    text=f"The platform rolled the command back: {outcome.output[:200]}",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            if outcome.failed or outcome.exit_code != 0:
                return ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                    failure="deterministic_command_failed",
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=f"exit=0\n{outcome.output[:2000]}",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario == "wait_once":
            # Asked for once per Run, not once per slice. The wait's own result
            # turn is in the transcript when the Scheduler wakes this Run, so
            # the second round sees it and moves on instead of waiting again.
            waited = any(
                isinstance(block, ToolResultBlock) and block.call_id == "wait-1"
                for message in request.messages
                for block in message.blocks
            )
            if not waited:
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="The deterministic scenario waits for something outside it.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="wait-1",
                            name="platform.wait",
                            arguments={"seconds": WAIT_SECONDS},
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text="The deterministic scenario was woken and finished.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario == "skill_once":
            # The skill drill: the Run input names the skill, the model loads
            # it, and its answer is the document's own text. Nothing here
            # touches a sandbox, so this scenario runs anywhere.
            #
            # Only results after the last user message count, for the same
            # reason `shell_from_input` says: a shared Session's transcript
            # already carries earlier Runs' results under this call id.
            results = tuple(
                block
                for message in request.messages[_last_user_index(request) + 1 :]
                for block in message.blocks
                if isinstance(block, ToolResultBlock) and block.call_id == "skill-load-1"
            )
            if not results:
                name = _last_user_text(request) or _first_skill_name(request)
                if not name:
                    return ModelResponse(
                        stop_reason=StopReason.FAILED,
                        text="",
                        failure="deterministic_no_skill_named",
                    )
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Loading the skill the drill named.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="skill-load-1",
                            name="skill.load",
                            arguments={"skill": name},
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            loaded = results[-1]
            if loaded.failed:
                return ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                    failure="deterministic_skill_load_refused",
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=f"skill loaded\n{loaded.output[:2000]}",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario in ("http_once", "mcp_once"):
            # §16.2 end to end without a real model: call the first HTTP
            # operation this Version bound, and answer with what came back. The
            # Run input may name a different one, which is how the same
            # scenario drills a refusal — an unbound name, or a write.
            called = tuple(
                block
                for message in request.messages[_last_user_index(request) + 1 :]
                for block in message.blocks
                if isinstance(block, ToolResultBlock) and block.call_id == "http-1"
            )
            if not called:
                name = _tool_call_name(request, scenario)
                if not name:
                    return ModelResponse(
                        stop_reason=StopReason.FAILED,
                        text="",
                        failure="deterministic_no_http_tool_bound",
                    )
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Calling the operation the drill named.",
                    tool_calls=(
                        ToolCallBlock(call_id="http-1", name=name, arguments={}),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            answer = called[-1]
            # A refused call still completes the Run here. The drill is about
            # what the tool result says, and a failed Run would hide it behind
            # a state transition.
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=f"http answered\n{answer.output[:2000]}",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario == "propose_once":
            # §15.3 end to end without a sandbox and without a real model: the
            # Run loads the skill it was given, proposes one line added to it,
            # and stops. What it proposes is deliberately dull — the drill is
            # about the governance path, not about the content.
            proposed = tuple(
                block
                for message in request.messages[_last_user_index(request) + 1 :]
                for block in message.blocks
                if isinstance(block, ToolResultBlock) and block.call_id == "propose-1"
            )
            if not proposed:
                name = _last_user_text(request) or _first_skill_name(request)
                if not name:
                    return ModelResponse(
                        stop_reason=StopReason.FAILED,
                        text="",
                        failure="deterministic_no_skill_named",
                    )
                return ModelResponse(
                    stop_reason=StopReason.TOOL_CALL,
                    text="Suggesting one line for a person to review.",
                    tool_calls=(
                        ToolCallBlock(
                            call_id="propose-1",
                            name="skill.propose",
                            arguments={
                                "skill": name,
                                "files": [
                                    {
                                        "path": "SKILL.md",
                                        "content": _proposed_manifest(name),
                                    }
                                ],
                            },
                        ),
                    ),
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                )
            outcome = proposed[-1]
            if outcome.failed:
                return ModelResponse(
                    stop_reason=StopReason.FAILED,
                    text="",
                    input_tokens=TOKENS_PER_ROUND // 2,
                    output_tokens=TOKENS_PER_ROUND // 2,
                    failure="deterministic_proposal_refused",
                )
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=f"proposal opened\n{outcome.output[:2000]}",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        if scenario == "fail_replay_safe":
            return ModelResponse(
                stop_reason=StopReason.FAILED,
                text="The deterministic scenario stopped at a replay-safe checkpoint.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
                replay_safe=True,
                external_effect_unknown=False,
            )
        if scenario == "continue_once" and request.round_index <= 1:
            return ModelResponse(
                stop_reason=StopReason.CONTINUE,
                text="The deterministic scenario needs one more round.",
                input_tokens=TOKENS_PER_ROUND // 2,
                output_tokens=TOKENS_PER_ROUND // 2,
            )
        return ModelResponse(
            stop_reason=StopReason.COMPLETED,
            text="The deterministic scenario finished.",
            input_tokens=TOKENS_PER_ROUND // 2,
            output_tokens=TOKENS_PER_ROUND // 2,
        )


def _last_user_index(request: ModelRequest) -> int:
    for index in range(len(request.messages) - 1, -1, -1):
        if request.messages[index].role == "user":
            return index
    return -1


#: What `propose_once` suggests. A whole package, because a proposal is the
#: files the skill should end up with rather than a patch — the drill would not
#: prove much if it sent something the catalog could not store.
PROPOSED_LINE = "Check the dashboard before you start."


def _proposed_manifest(name: str) -> str:
    """A whole package, because a proposal is files rather than a patch."""
    lines = (
        "---",
        f"name: {name}",
        "description: How this company takes a machine out of rotation before a deploy.",
        "---",
        "",
        f"# {name}",
        "",
        PROPOSED_LINE,
    )
    return "\n".join(lines) + "\n"


def _tool_call_name(request: ModelRequest, scenario: str) -> str:
    """Which generated tool the drill should call.

    The Run input wins when it names one, so a single scenario can drill the
    answer, an unbound name and a refused write. With no input it falls back to
    the first tool of the right family the Version bound.
    """
    prefix = HTTP_PREFIX if scenario == "http_once" else MCP_PREFIX
    asked = _last_user_text(request).strip()
    if asked.startswith(f"{prefix}."):
        return asked
    for schema in request.tools:
        function = schema.get("function")
        if not isinstance(function, dict):
            continue
        name = str(cast(dict[str, Any], function).get("name", ""))
        if name.startswith(f"{prefix}."):
            return name
    return ""


def _first_skill_name(request: ModelRequest) -> str:
    """The name out of the first summary line, or nothing.

    The summaries the Worker builds read ``- name: description``, and the
    drill's fallback is to load whichever skill the Version bound first — so a
    Run started with no input still exercises the path.
    """
    for summary in request.skill_summaries:
        head = summary.lstrip("- ").split(":", 1)[0].strip()
        if head:
            return head
    return ""


def _last_user_text(request: ModelRequest) -> str:
    last = _last_user_index(request)
    if last < 0:
        return ""
    for block in request.messages[last].blocks:
        if isinstance(block, TextBlock):
            return block.text.strip()
    return ""
