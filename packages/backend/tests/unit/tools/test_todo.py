"""`todo.write`: the model hands in its whole task list, and gets it back rendered.

The whole list rather than a patch: a model that sends "mark 3 done" has to
remember what 1 and 2 were, which is the thing it is using the list to not
have to remember.
"""

from typing import Any

import pytest
from tiny_hermes.runs.domain.context_budget import TODO_WRITE_TOOL
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    PLATFORM_TOOLS,
    TODO_WRITE_SCHEMA,
    UNTRIMMED_TOOLS,
    schemas_for,
)
from tiny_hermes.tools.domain.todo import (
    MAX_TODO_CONTENT_CHARS,
    MAX_TODO_ITEMS,
    TodoArgumentsInvalid,
    render_todos,
    todo_counts,
    todos_of,
)


def _call(items: Any) -> ToolCallBlock:
    return ToolCallBlock(call_id="t1", name="todo.write", arguments={"items": items})


def _item(identifier: str, content: str, status: str = "pending") -> dict[str, str]:
    return {"id": identifier, "content": content, "status": status}


def test_a_list_is_rendered_with_each_status_marked() -> None:
    items = todos_of(
        _call(
            [
                _item("1", "read the failing test", "completed"),
                _item("2", "fix the parser", "in_progress"),
                _item("3", "run the suite"),
                _item("4", "rewrite the docs", "cancelled"),
            ]
        )
    )

    rendered = render_todos(items)

    assert rendered.splitlines() == [
        "[x] 1. read the failing test",
        "[>] 2. fix the parser",
        "[ ] 3. run the suite",
        "[-] 4. rewrite the docs",
    ]
    assert todo_counts(items) == {"total": 4, "completed": 1, "in_progress": 1}


def test_an_empty_list_clears_it() -> None:
    assert todos_of(_call([])) == ()
    assert render_todos(()) == "(the task list is empty)"


@pytest.mark.parametrize(
    "items",
    [
        None,
        "1. do it",
        [{"id": "1", "content": "x"}],
        [_item("1", "x", "blocked")],
        [_item("1", "x"), _item("1", "y")],
        [_item("", "x")],
        [_item("1", "")],
        [_item("1", "x" * (MAX_TODO_CONTENT_CHARS + 1))],
        [_item(str(index), "x") for index in range(MAX_TODO_ITEMS + 1)],
        [{**_item("1", "x"), "priority": "high"}],
    ],
)
def test_a_list_the_platform_cannot_keep_is_refused(items: Any) -> None:
    with pytest.raises(TodoArgumentsInvalid):
        todos_of(_call(items))


def test_the_tool_is_a_platform_tool_that_is_never_trimmed() -> None:
    assert TODO_WRITE_TOOL == "todo.write"
    assert TODO_WRITE_TOOL in IMPLEMENTED_TOOLS
    assert TODO_WRITE_TOOL in PLATFORM_TOOLS
    assert TODO_WRITE_TOOL in UNTRIMMED_TOOLS
    assert schemas_for(("todo.write",)) == [TODO_WRITE_SCHEMA]
    assert TODO_WRITE_SCHEMA["function"]["name"] == TODO_WRITE_TOOL
