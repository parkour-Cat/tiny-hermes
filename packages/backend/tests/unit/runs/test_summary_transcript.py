"""What the summarizer is shown of a tool round (§7.4.2 v2.10).

The summary needs to know a command ran and what it broadly returned, not
every byte of it; the full output is still in the session transcript.
"""

from uuid import uuid4

# Private and asserted on directly, as `test_summary_widening.py` does with
# `_summary_holds`: the transcript is what the summarizer is shown, and no
# public surface exposes it short of driving a Run.
from tiny_hermes.runs.application.worker import (
    _transcript_text,  # pyright: ignore[reportPrivateUsage]
)
from tiny_hermes.runs.domain.context_budget import SUMMARY_TOOL_RESULT_CHARS
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    StoredMessage,
    ToolCallBlock,
    ToolResultBlock,
)


def test_a_long_tool_result_is_shortened_for_the_summarizer() -> None:
    output = "A" * SUMMARY_TOOL_RESULT_CHARS + "B" * 50_000
    covered = (
        StoredMessage(
            id=uuid4(),
            sequence=1,
            message=CanonicalMessage(
                role="assistant",
                blocks=(ToolCallBlock(call_id="c1", name="shell.exec", arguments={"c": "cat"}),),
            ),
        ),
        StoredMessage(
            id=uuid4(),
            sequence=2,
            message=CanonicalMessage(
                role="tool",
                blocks=(ToolResultBlock(call_id="c1", output=output, exit_code=0, failed=False),),
            ),
        ),
    )

    said = _transcript_text(covered)

    assert "A" * SUMMARY_TOOL_RESULT_CHARS in said
    assert "B" * 100 not in said
    assert str(len(output)) in said
