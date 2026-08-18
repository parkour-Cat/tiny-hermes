"""Turning a bound operation into a tool the model is told about, and back.

Product design §16.2, both steps, for a family of tools rather than a fixed
list. The first step is `schemas_for_operations`: the model hears about exactly
the operations its Version bound, under names it can type. The second is
`http_call_of`: what the model actually asked for, checked against the
operation's own parameters — never against the name it typed.

Pure. Composing the request is arithmetic on strings; sending it is somebody
else's job, and that somebody is the platform rather than the sandbox, so the
call passes the outbound boundary like every other request this process makes.

**A write does not run here.** §16.3 requires an approval before an external
write and approvals arrive in the next step of the plan, so this module answers
a write with a named refusal. That refusal is temporary and the test that pins
it says so — but it is the only correct behaviour while the capability it
depends on does not exist.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.openapi import Operation

#: The prefix every generated tool name carries. A model reading its tool list
#: can tell an HTTP call from a platform tool without being told, and the
#: registry can route on it without a lookup.
HTTP_PREFIX = "http"

#: What one response may bring back into the conversation. The same ceiling and
#: the same reason as `skill.load`: a model handed half a document cannot tell
#: it is holding half, so an oversized answer is refused with its size rather
#: than truncated.
MAX_RESPONSE_BYTES = 65_536

_ARGUMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class BoundOperation:
    """One operation an Agent may call, with everything the call needs.

    Assembled by the Worker from the Version's bindings and the catalog. The
    tool name is derived rather than stored, so a rename of the tool cannot
    leave a binding pointing at a name nothing generates.
    """

    tool_name: str
    version_id: UUID
    base_url: str
    credential_ref: str | None
    operation: Operation

    @property
    def call_name(self) -> str:
        return f"{HTTP_PREFIX}.{self.tool_name}.{self.operation.operation_id}"


@dataclass(frozen=True)
class HttpRequestPlan:
    """A request, composed and not yet sent."""

    method: str
    url: str
    body: bytes | None
    #: Only what the operation asked for. The credential is added where the
    #: request is sent, so nothing that passes through the model's side of the
    #: platform has ever held one.
    headers: dict[str, str]
    read_only: bool


class HttpCallRefused(Exception):
    """A call this platform will not make, and why.

    `reason` is a short code the Worker turns into a tool result; `detail`
    names the argument, so a model that sent one wrong thing is told which.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def schemas_for_operations(bound: list[BoundOperation]) -> list[dict[str, Any]]:
    """Step one: what the model is told exists.

    Only the operations this Version bound, and only the parameters this
    platform actually sends. An operation the Agent did not bind is not
    described here and is refused by `http_call_of` regardless — the schema
    list is not a control, and this is the half that is merely honest.
    """
    schemas: list[dict[str, Any]] = []
    for entry in bound:
        operation = entry.operation
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in operation.parameters:
            properties[parameter.name] = {
                **parameter.schema,
                **(
                    {"description": parameter.description}
                    if parameter.description
                    else {}
                ),
            }
            if parameter.required:
                required.append(parameter.name)
        if operation.body_schema is not None:
            properties["body"] = operation.body_schema
            required.append("body")
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": entry.call_name,
                    "description": _description(entry),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas


def http_call_of(call: ToolCallBlock, bound: list[BoundOperation]) -> HttpRequestPlan:
    """Step two: what actually runs, against the arguments that arrived.

    The name the model typed selects a binding, and everything after that is
    checked against the *operation*: an argument the operation does not declare
    is refused rather than passed on, because a request carrying a parameter
    nobody described is a request nobody reviewed.
    """
    entry = next((item for item in bound if item.call_name == call.name), None)
    if entry is None:
        # The same refusal an unbound tool gets, and for the same reason: what
        # the model may reach is what the Version bound.
        raise HttpCallRefused("tool_not_authorized", call.name)
    operation = entry.operation
    declared = {parameter.name: parameter for parameter in operation.parameters}
    arguments = cast(dict[str, object], dict(call.arguments))
    body = arguments.pop("body", None)

    unknown = sorted(set(arguments) - set(declared))
    if unknown:
        raise HttpCallRefused("invalid_arguments", ",".join(unknown))
    missing = sorted(
        name
        for name, parameter in declared.items()
        if parameter.required and name not in arguments
    )
    if missing:
        raise HttpCallRefused("invalid_arguments", ",".join(missing))
    for name in arguments:
        if not _ARGUMENT_NAME.match(name):  # pragma: no cover - `declared` bounds it
            raise HttpCallRefused("invalid_arguments", name)

    path = operation.path
    query: dict[str, str] = {}
    for name, value in arguments.items():
        rendered = _rendered(name, value)
        if declared[name].location == "path":
            # Percent-encoded, and `/` with it: a value that could add a path
            # segment would let an argument choose a different endpoint than
            # the operation names.
            path = path.replace("{" + name + "}", quote(rendered, safe=""))
        else:
            query[name] = rendered

    if operation.body_schema is not None and body is None:
        raise HttpCallRefused("invalid_arguments", "body")
    if body is not None and operation.body_schema is None:
        raise HttpCallRefused("invalid_arguments", "body")

    url = _joined(entry.base_url, path)
    if query:
        url = f"{url}?{urlencode(query)}"
    return HttpRequestPlan(
        method=operation.method,
        url=url,
        body=None if body is None else json.dumps(body).encode("utf-8"),
        headers=(
            {"Content-Type": "application/json"} if body is not None else {}
        ),
        read_only=operation.read_only,
    )


def _description(entry: BoundOperation) -> str:
    summary = entry.operation.summary or entry.operation.operation_id
    note = "" if entry.operation.read_only else " This changes data at the far end."
    return f"{summary}{note}"


def _rendered(name: str, value: object) -> str:
    """One argument as a string, refusing what has no obvious spelling.

    A dict or a list in a path or query is a shape this platform would have to
    invent an encoding for, and an invented encoding is one the far end did not
    agree to.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise HttpCallRefused("invalid_arguments", name)


def _joined(base_url: str, path: str) -> str:
    """The operation's path under the registered base, and nowhere else.

    Built by hand rather than with `urljoin`, which treats a path beginning
    with `/` as absolute and would throw away a base URL's own prefix — turning
    `https://api.example.com/v2` plus `/orders` into `https://api.example.com/orders`.
    """
    parts = urlsplit(base_url)
    prefix = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{prefix}{path}"
