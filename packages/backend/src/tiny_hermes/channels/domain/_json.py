"""Reading fields out of a payload this module did not build.

`isinstance(value, dict)` narrows to `dict[Unknown, Unknown]`, so every read
of a decoded JSON object needs a cast to say what JSON already guarantees:
object keys are strings. These three keep that statement in one place
instead of at every field, which also means a missing field is `None`
everywhere rather than `None` in some readers and a `KeyError` in others.
"""

from typing import Any, cast


def object_at(container: dict[str, Any], key: str) -> dict[str, Any] | None:
    value: object = container.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def string_at(container: dict[str, Any], key: str) -> str | None:
    """Empty is treated as absent: a field carrying `""` is not a value this
    platform can act on, and letting it through only moves the failure."""
    value: object = container.get(key)
    return value if isinstance(value, str) and value != "" else None


def int_at(container: dict[str, Any], key: str, default: int) -> int:
    value: object = container.get(key)
    # `bool` is an `int` in Python and never a position or a count here.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def strings_at(container: dict[str, Any], key: str) -> tuple[str, ...]:
    value: object = container.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in cast("list[object]", value) if isinstance(item, str))
