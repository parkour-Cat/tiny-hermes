"""The model's task list (§16.1, v2.12).

There is no table behind it. The list is the last `todo.write` result in the
transcript: a persistent Session hands that to the next Run as it hands over
everything else, and compaction puts it back (`context_budget._put_back`).

Pure: validation and rendering only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.runs.domain.models import ToolCallBlock

#: Enough for a long task broken into steps; a list longer than this is a
#: backlog, and a backlog re-sent in full on every update is Token spent
#: saying the same thing again.
MAX_TODO_ITEMS = 30
MAX_TODO_CONTENT_CHARS = 200

_MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}
_FIELDS = frozenset({"id", "content", "status"})


class TodoArgumentsInvalid(ValueError):
    """A list the platform will not keep, with a reason the model can act on."""


@dataclass(frozen=True)
class TodoItem:
    id: str
    content: str
    status: str


def todos_of(call: ToolCallBlock) -> tuple[TodoItem, ...]:
    """The whole list this call hands in, or why it cannot be kept."""
    items: Any = call.arguments.get("items")
    if not isinstance(items, list):
        raise TodoArgumentsInvalid("items must be a list")
    entries = cast(list[Any], items)
    if len(entries) > MAX_TODO_ITEMS:
        raise TodoArgumentsInvalid(f"at most {MAX_TODO_ITEMS} items")
    kept: list[TodoItem] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or frozenset(cast(dict[str, Any], entry)) != _FIELDS:
            raise TodoArgumentsInvalid("each item has exactly id, content and status")
        fields = cast(dict[str, Any], entry)
        identifier, content, status = fields["id"], fields["content"], fields["status"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise TodoArgumentsInvalid("each item needs a non-empty id")
        if identifier in seen:
            raise TodoArgumentsInvalid(f"id {identifier!r} is used twice")
        if not isinstance(content, str) or not content.strip():
            raise TodoArgumentsInvalid("each item needs non-empty content")
        if len(content) > MAX_TODO_CONTENT_CHARS:
            raise TodoArgumentsInvalid(f"content is at most {MAX_TODO_CONTENT_CHARS} characters")
        if status not in _MARKS:
            raise TodoArgumentsInvalid(f"status is one of {', '.join(_MARKS)}")
        seen.add(identifier)
        kept.append(TodoItem(id=identifier, content=content.strip(), status=status))
    return tuple(kept)


def render_todos(items: Sequence[TodoItem]) -> str:
    """The list as the model and a person reading the transcript both see it."""
    if not items:
        return "(the task list is empty)"
    return "\n".join(f"{_MARKS[item.status]} {item.id}. {item.content}" for item in items)


def todo_counts(items: Sequence[TodoItem]) -> dict[str, int]:
    """What the timeline says about the list: how far along, never its words."""
    return {
        "total": len(items),
        "completed": sum(item.status == "completed" for item in items),
        "in_progress": sum(item.status == "in_progress" for item in items),
    }
