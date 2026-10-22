"""A round the platform lost while a call was on its way out (§17.2, v2.17).

A call that may reach outside the sandbox is written down, with the round it
belongs to, in the transaction that charges it. If the Worker is gone before
the round is recorded, recovery puts the round into the transcript from this
record instead of asking the model again from the same history: asked again,
the model may well make the same write with different arguments, and nothing
at the far end could tell that the second one was a repeat.

What the far end did with the call is not known to anybody on this side. The
model is told so, and is the one placed to find out, since it can call the
API's reads.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.runs.domain.models import (
    Block,
    CanonicalMessage,
    ToolCallBlock,
    ToolResultBlock,
    message_from_document,
)
from tiny_hermes.tools.domain.files import changes_workspace

UNKNOWN = (
    "interrupted: the platform stopped while this call was going out, so whether "
    "it took effect is unknown; it may have. Before doing it again, check its "
    "effect if you can, for example by reading back what it would have changed."
)
SAME_KEY = (
    " Repeating exactly the same request (same operation, same arguments) sends it "
    "with the same Idempotency-Key as the interrupted one, so an API that honours "
    "that header applies it at most once. Any other request gets a new key."
)
NOT_COMPLETED = (
    "not completed: the platform stopped earlier in this round, so this call may "
    "not have run."
)
LATER = "The platform stopped later in this round."
# A round's changes to the data mount are committed after the round, and this
# one never got there: the next sandbox is restored from the last committed
# revision. A commit that did happen without the round being recorded leaves
# the Session's revision ahead of the checkpoint's; recovery tells nothing
# then, and §12 fails the Run.
NOT_SAVED = (
    " Its changes to /workspace/data, if any, were not saved: the workspace "
    "continues from the last saved revision."
)


@dataclass(frozen=True)
class InFlightRound:
    """The round as it stood when one of its calls was sent."""

    #: The model's turn: its words and every call it asked for.
    assistant: CanonicalMessage
    #: Answers to the calls before the one being sent, in order.
    finished: tuple[ToolResultBlock, ...]
    #: The call being sent.
    call_id: str
    #: Sent with the call, for an HTTP write.
    idempotency_key: str | None = None
    #: What the request was, for an HTTP write: an exact repeat is recognised
    #: by this, and takes the key again.
    fingerprint: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "assistant": self.assistant.document(),
            "finished": [block.document() for block in self.finished],
            "call_id": self.call_id,
            "idempotency_key": self.idempotency_key,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "InFlightRound":
        parts = cast(list[Any], document.get("finished") or [])
        finished = (
            message_from_document({"role": "tool", "parts": parts}).blocks if parts else ()
        )
        key = document.get("idempotency_key")
        fingerprint = document.get("fingerprint")
        return cls(
            assistant=message_from_document(cast(dict[str, Any], document["assistant"])),
            finished=tuple(block for block in finished if isinstance(block, ToolResultBlock)),
            call_id=str(document["call_id"]),
            idempotency_key=None if key is None else str(key),
            fingerprint=None if fingerprint is None else str(fingerprint),
        )


def told_after_interruption(
    sending: InFlightRound,
) -> tuple[CanonicalMessage, CanonicalMessage]:
    """The model's turn, and an answer for every call in it.

    Every call gets one, in order: a provider refuses a transcript in which a
    call has no answer.
    """
    calls = [block for block in sending.assistant.blocks if isinstance(block, ToolCallBlock)]
    finished = {block.call_id: block for block in sending.finished}
    answers: list[Block] = []
    reached = False
    for call in calls:
        if call.call_id == sending.call_id:
            reached = True
            output = UNKNOWN + (SAME_KEY if sending.idempotency_key is not None else "")
            if changes_workspace(call.name):
                output += NOT_SAVED
            answers.append(_failed(call, output))
        elif reached or call.call_id not in finished:
            output = NOT_COMPLETED + (NOT_SAVED if changes_workspace(call.name) else "")
            answers.append(_failed(call, output))
        else:
            done = finished[call.call_id]
            if changes_workspace(call.name):
                done = ToolResultBlock(
                    call_id=done.call_id,
                    output=f"{done.output}\n\n[{LATER}{NOT_SAVED}]",
                    exit_code=done.exit_code,
                    failed=done.failed,
                )
            answers.append(done)
    return sending.assistant, CanonicalMessage("tool", tuple(answers))


def _failed(call: ToolCallBlock, output: str) -> ToolResultBlock:
    return ToolResultBlock(call_id=call.call_id, output=output, exit_code=1, failed=True)
