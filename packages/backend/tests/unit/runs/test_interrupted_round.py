"""What the model is told about a round the platform lost halfway through.

§17.2, v2.17: the round is written into the transcript as it stood when the
Worker went, and the call that was going out is answered "result unknown"
rather than replayed or turned into a failed Run.
"""

from tiny_hermes.runs.domain.interruption import InFlightRound, told_after_interruption
from tiny_hermes.runs.domain.models import (
    CanonicalMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _round(
    *calls: ToolCallBlock,
    finished: tuple[ToolResultBlock, ...] = (),
    sending: str = "c1",
    key: str | None = None,
) -> InFlightRound:
    return InFlightRound(
        assistant=CanonicalMessage("assistant", (TextBlock(text="on it"), *calls)),
        finished=finished,
        call_id=sending,
        idempotency_key=key,
    )


def _call(call_id: str, name: str = "http.orders.createOrder") -> ToolCallBlock:
    return ToolCallBlock(call_id=call_id, name=name, arguments={})


def _results(round_: InFlightRound) -> dict[str, ToolResultBlock]:
    assistant, tool = told_after_interruption(round_)
    assert assistant == round_.assistant
    assert tool.role == "tool"
    return {
        block.call_id: block for block in tool.blocks if isinstance(block, ToolResultBlock)
    }


def test_the_call_that_was_going_out_is_answered_unknown() -> None:
    results = _results(_round(_call("c1")))

    assert results["c1"].failed is True
    assert "unknown" in results["c1"].output
    # Told to look before acting again: the platform cannot know, the model
    # may be able to ask.
    assert "check" in results["c1"].output


def test_every_call_is_answered_once_and_in_order() -> None:
    """A provider refuses a transcript in which a call has no answer."""
    calls = (_call("c1", "file.read"), _call("c2"), _call("c3", "file.read"))
    done = (ToolResultBlock(call_id="c1", output="hello", exit_code=0, failed=False),)

    _, tool = told_after_interruption(_round(*calls, finished=done, sending="c2"))

    assert [block.call_id for block in tool.blocks if isinstance(block, ToolResultBlock)] == [
        "c1",
        "c2",
        "c3",
    ]


def test_a_finished_read_keeps_its_answer() -> None:
    done = ToolResultBlock(call_id="c1", output="hello", exit_code=0, failed=False)

    results = _results(
        _round(_call("c1", "file.read"), _call("c2"), finished=(done,), sending="c2")
    )

    assert results["c1"] == done


def test_a_finished_write_to_the_workspace_says_the_round_was_not_saved() -> None:
    """Recovery starts from the last committed revision, which this round's
    changes were never part of."""
    done = ToolResultBlock(call_id="c1", output="wrote a.txt", exit_code=0, failed=False)

    results = _results(
        _round(_call("c1", "file.write"), _call("c2"), finished=(done,), sending="c2")
    )

    assert results["c1"].output.startswith("wrote a.txt")
    assert "/workspace/data" in results["c1"].output
    assert "not saved" in results["c1"].output


def test_a_call_after_the_one_going_out_is_not_completed() -> None:
    results = _results(_round(_call("c1"), _call("c2", "file.write"), sending="c1"))

    assert results["c2"].failed is True
    assert "not completed" in results["c2"].output


def test_with_a_key_the_model_is_told_an_exact_repeat_reuses_it() -> None:
    results = _results(_round(_call("c1"), key="k-1"))

    assert "Idempotency-Key" in results["c1"].output
    assert "exactly the same" in results["c1"].output


def test_without_a_key_nothing_is_promised_about_a_repeat() -> None:
    results = _results(_round(_call("c1", "mcp.crm.create")))

    assert "Idempotency-Key" not in results["c1"].output


def test_the_round_survives_being_stored() -> None:
    round_ = _round(
        _call("c1", "file.read"),
        _call("c2"),
        finished=(ToolResultBlock(call_id="c1", output="x", exit_code=0, failed=False),),
        sending="c2",
        key="k-1",
    )

    assert InFlightRound.from_document(round_.document()) == round_
