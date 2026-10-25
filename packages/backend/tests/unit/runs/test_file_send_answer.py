"""What the model is told when it sends a file (§1.27, v2.27)."""

from typing import Any
from uuid import uuid4

import pytest
from tiny_hermes.runs.application.file_sends import (
    MAX_FILES_PER_RUN,
    FileSendRefused,
    FileSent,
)
from tiny_hermes.runs.application.tool_answers import answer_file_send
from tiny_hermes.runs.domain.models import RunEventType, ToolCallBlock


class _Sender:
    def __init__(self, outcome: FileSent | FileSendRefused) -> None:
        self._outcome = outcome
        self.asked: list[str] = []

    async def send(self, **kwargs: Any) -> FileSent:
        self.asked.append(kwargs["path"])
        if isinstance(self._outcome, FileSendRefused):
            raise self._outcome
        return self._outcome


class _Context:
    def __init__(self, tools: tuple[str, ...] = ("file.send",)) -> None:
        self.tools = tools
        self.run_id = uuid4()


CALL = ToolCallBlock(call_id="s1", name="file.send", arguments={"path": "out/周报.xlsx"})


async def _answer(sender: Any, *, sent_earlier: int = 0, tools: tuple[str, ...] = ("file.send",)):
    return await answer_file_send(
        sender,
        _Context(tools),  # pyright: ignore[reportArgumentType]
        CALL,
        workspace_id=uuid4(),
        session_id=uuid4(),
        sent_earlier=sent_earlier,
    )


async def test_a_sent_file_is_said_and_recorded_by_name_and_size_only() -> None:
    result, event = await _answer(_Sender(FileSent(name="周报.xlsx", size=2048, delivered=True)))

    assert not result.failed and "周报.xlsx" in result.output
    assert event is not None and event.event_type is RunEventType.FILE_SENT
    assert event.payload == {"path": "out/周报.xlsx", "size": 2048, "delivered": True}


async def test_outside_feishu_the_model_is_told_where_the_person_finds_it() -> None:
    result, _ = await _answer(_Sender(FileSent(name="周报.xlsx", size=2048, delivered=False)))

    assert not result.failed
    assert "not in Feishu" in result.output and "Run's files" in result.output


async def test_a_refusal_reaches_the_model_with_its_reason() -> None:
    sender = _Sender(FileSendRefused("not_found", "out/周报.xlsx is not in the saved workspace"))

    result, event = await _answer(sender)

    assert result.failed and "not_found" in result.output and event is None


async def test_the_eleventh_file_is_refused_before_anything_is_read() -> None:
    sender = _Sender(FileSent(name="x", size=1, delivered=True))

    result, event = await _answer(sender, sent_earlier=MAX_FILES_PER_RUN)

    assert result.failed and str(MAX_FILES_PER_RUN) in result.output
    assert sender.asked == [] and event is None


@pytest.mark.parametrize("sender", [None])
async def test_a_deployment_that_cannot_send_says_so(sender: Any) -> None:
    result, _ = await _answer(sender)

    assert result.failed and "does not send files" in result.output


async def test_an_agent_that_did_not_bind_it_is_refused() -> None:
    result, _ = await _answer(_Sender(FileSent(name="x", size=1, delivered=True)), tools=())

    assert result.failed
