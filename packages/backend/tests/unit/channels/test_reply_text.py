"""What a channel says back when a Run finishes.

The inbound half has been provable without a tenant since §574's claim
landed. This is the outbound half's provable part: given a terminal Run and
whatever the Agent said, decide the words. Sending them is the vendor's
business and is tested separately against a stub; *choosing* them is this
platform's own decision and belongs somewhere a test can pin it.

The case that matters most here is the one with no text. A Run that
completed having said nothing is not an error, and a channel that stays
quiet for it is indistinguishable — to the person who sent the message —
from a platform that dropped their message on the floor. That silence is
this repository's own recurring bug wearing a chat interface.
"""

import pytest
from tiny_hermes.channels.domain.reply import MAX_REPLY_CHARS, reply_for
from tiny_hermes.runs.domain.models import RunState


def test_a_completed_run_replies_with_what_the_agent_said() -> None:
    assert reply_for(state=RunState.COMPLETED, said="上周有 12 单。") == "上周有 12 单。"


def test_a_completed_run_that_said_nothing_still_replies() -> None:
    """Silence is the failure mode, not the empty string.

    Whitespace counts as nothing: an Agent whose last turn was a newline has
    said nothing to a person, and stripping here rather than at the sender
    keeps that judgement in one place.
    """
    reply = reply_for(state=RunState.COMPLETED, said="  \n ")

    assert reply is not None
    assert reply.strip() != ""


def test_a_failed_run_says_so_and_says_why() -> None:
    """`failure_reason` is on this project's own list of things written and
    never rendered. A chat reply that omitted it would put it back."""
    reply = reply_for(
        state=RunState.FAILED, said="", failure_reason="model_endpoint_unreachable"
    )

    assert reply is not None
    assert "model_endpoint_unreachable" in reply


def test_a_failed_run_with_no_recorded_reason_still_says_it_failed() -> None:
    reply = reply_for(state=RunState.FAILED, said="")

    assert reply is not None
    assert reply.strip() != ""


def test_a_cancelled_run_says_so_rather_than_reporting_success() -> None:
    completed = reply_for(state=RunState.COMPLETED, said="做完了")
    cancelled = reply_for(state=RunState.CANCELLED, said="做完了")

    assert cancelled is not None
    assert cancelled != completed


@pytest.mark.parametrize(
    "state",
    [
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.WAITING_APPROVAL,
        RunState.WAITING_EXTERNAL,
        RunState.PAUSED,
        RunState.CANCELLING,
        RunState.INTERRUPTED,
    ],
)
def test_a_run_that_has_not_finished_has_nothing_to_say_yet(state: RunState) -> None:
    """`None` means "not yet", never "nothing".

    The dispatcher treats `None` as leave-it-alone and comes back next scan.
    A non-terminal Run answering with words would send a person a reply in
    the middle of the work, and — worse — the dispatcher would stamp the
    delivery replied, so the real answer would never arrive.
    """
    assert reply_for(state=state, said="半句话") is None


def test_a_very_long_answer_is_cut_rather_than_refused() -> None:
    """The cap is this platform's, not a measured vendor limit.

    Feishu documents a size limit on message content that this repository
    has not verified against the service, so the number here is chosen for
    the reader rather than derived from the API: a chat window is not where
    a hundred-kilobyte dump belongs. Refusing to send would be worse than
    truncating — the person would get nothing at all.
    """
    reply = reply_for(state=RunState.COMPLETED, said="长" * (MAX_REPLY_CHARS * 3))

    assert reply is not None
    assert len(reply) <= MAX_REPLY_CHARS
    assert reply.startswith("长")
    # Cut, and *said* to be cut. A truncation the reader cannot see is a
    # reply that looks like the Agent stopped mid-sentence.
    assert reply.rstrip()[-1] != "长"
