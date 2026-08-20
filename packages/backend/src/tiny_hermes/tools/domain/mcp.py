"""What an MCP server advertises, and what an Agent may be told about it.

Product design §16.2. The pure half: a `tools/list` answer goes in, a list of
bindable tools comes out, or a refusal naming what is wrong with it. Nothing
here opens a socket.

Three rules run through the file.

**A binding is a subset that was written down.** §16.2 forbids handing a model
everything a server discovered, and this module has no way to express "all" —
`bound_tools` takes names and returns only those. A field that could say "all"
would be written as "all" on the first day, and a server that later advertised
forty more tools would widen a published Agent with nobody publishing anything.

**A server's answer is data, never instruction.** A tool description arrives
from somebody else's process and goes into the model's context. It is treated
exactly like skill text: reference material the model reads, not orders the
platform follows. Nothing here executes, resolves or expands anything a server
said.

**The schemas are measured, never truncated.** A bound subset that does not fit
the segment that carries it stops the Run. Cutting a schema down would leave a
model calling a tool with arguments the far end never agreed to.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from tiny_hermes.tools.domain.openapi import estimated_tokens_of

#: The prefix every generated tool name carries, the twin of `HTTP_PREFIX`. A
#: model reading its tool list can tell an MCP call from an HTTP one, and the
#: Worker can route on it without a lookup.
MCP_PREFIX = "mcp"

#: What one `tools/list` answer may weigh. The same reasoning as an OpenAPI
#: document's ceiling: large enough for a real server, small enough that
#: parsing one is not a memory decision.
MAX_CAPABILITY_BYTES = 1024 * 1024

#: How many tools one server may advertise. A server offering more than this is
#: not one an administrator can review, and reviewing is the whole control.
MAX_TOOLS = 128

#: What one tool's name may look like. It becomes part of a name a model types
#: back, so a name needing quoting is a name that will arrive misquoted.
NAME_MAX_LENGTH = 128

#: What one MCP call may bring back into the conversation. The same ceiling and
#: the same reason as `skill.load` and an HTTP response: a model handed half a
#: document cannot tell it is holding half.
MAX_RESULT_BYTES = 65_536


class McpRefused(Exception):
    """An answer this platform will not turn into tools, and why."""


@dataclass(frozen=True)
class McpTool:
    """One tool a server advertises.

    `input_schema` is the server's own JSON Schema, kept as it arrived. This
    platform does not rewrite it: what the model is told and what the far end
    validates against must be the same document, and a rewrite is a place for
    them to differ.
    """

    name: str
    description: str | None
    input_schema: dict[str, Any]

    @property
    def content_hash(self) -> str:
        """This tool as this platform would describe it, hashed.

        Per tool rather than per server, because §16.2's revalidation asks
        about the *bound subset*: a server adding an unrelated tool must not
        invalidate a binding that never named it.
        """
        return hashlib.sha256(
            json.dumps(
                {
                    "name": self.name,
                    "description": self.description,
                    "input_schema": self.input_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class McpCapabilities:
    """Everything one server said it can do, at one moment."""

    tools: tuple[McpTool, ...]
    #: The whole advertisement, hashed. What an administrator compares to see
    #: that a server changed at all; the per-tool hashes say what changed.
    content_hash: str

    def tool(self, name: str) -> McpTool | None:
        return next((item for item in self.tools if item.name == name), None)


def parse_capabilities(payload: str | bytes) -> McpCapabilities:
    """A `tools/list` answer, as this platform is willing to read it.

    Refuses rather than repairs. A tool with no name cannot be bound (the name
    is the key a binding is written against), a duplicate name would make two
    tools one, and a schema that is not an object is one the model cannot be
    told about honestly.
    """
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > MAX_CAPABILITY_BYTES:
        raise McpRefused(
            f"the capability list is {len(raw)} bytes, over {MAX_CAPABILITY_BYTES}"
        )
    try:
        document = json.loads(raw)
    except ValueError as broken:
        raise McpRefused(f"the capability list is not JSON: {broken}") from broken
    if not isinstance(document, dict):
        raise McpRefused("a capability list is a JSON object")
    body = cast(dict[str, Any], document)
    # Both shapes: a bare `tools/list` result and a JSON-RPC envelope around
    # one. Accepting both is not leniency about content — it is the difference
    # between a transport and a document, and only the document is read here.
    listed = body.get("tools")
    if listed is None and isinstance(body.get("result"), dict):
        listed = cast(dict[str, Any], body["result"]).get("tools")
    if not isinstance(listed, list):
        raise McpRefused("a capability list has a `tools` array")
    entries = cast(list[Any], listed)
    if len(entries) > MAX_TOOLS:
        raise McpRefused(f"{len(entries)} tools advertised, over {MAX_TOOLS}")

    tools: list[McpTool] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise McpRefused("each advertised tool is an object")
        tool = _tool(cast(dict[str, Any], entry))
        if tool.name in seen:
            raise McpRefused(f"{tool.name} is advertised twice")
        seen.add(tool.name)
        tools.append(tool)
    ordered = tuple(sorted(tools, key=lambda item: item.name))
    return McpCapabilities(tools=ordered, content_hash=_hash(ordered))


def bound_tools(
    capabilities: McpCapabilities, names: Sequence[str]
) -> tuple[McpTool, ...]:
    """Only the named tools, in the order they were named.

    A name the server no longer advertises is left out rather than raising: a
    Run that lost one of four tools is better off than one that cannot start,
    and calling the missing name is refused by the second authorization check
    regardless.

    There is deliberately no way to ask for all of them. See the module
    docstring.
    """
    found = [capabilities.tool(name) for name in names]
    return tuple(tool for tool in found if tool is not None)


def schemas_for_tools(server: str, tools: Sequence[McpTool]) -> list[dict[str, Any]]:
    """Step one of §16.2: what the model is told exists.

    The server's own name is part of the call name, so two servers offering a
    `search` do not fight over it — the same reason an HTTP tool's name is in
    `http.<tool>.<operation>`.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": call_name(server, tool.name),
                "description": tool.description or tool.name,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def call_name(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}.{server}.{tool}"


def estimated_tokens(server: str, tools: Sequence[McpTool]) -> int:
    """What telling a model about this subset costs.

    The same estimator the OpenAPI side uses, so "how big is this binding" has
    one answer whichever kind of tool is asking.
    """
    return estimated_tokens_of(
        [
            {
                "name": call_name(server, tool.name),
                "description": tool.description,
                "schema": tool.input_schema,
            }
            for tool in tools
        ]
    )


@dataclass(frozen=True)
class SchemaBudget:
    """Whether a bound subset fits the segment that carries it."""

    estimate: int
    allowance: int

    @property
    def fits(self) -> bool:
        return self.estimate <= self.allowance


def fits_schema_budget(estimate: int, allowance: int) -> SchemaBudget:
    """Measured, never truncated.

    Cutting a schema down to fit would leave a model calling a tool with
    arguments the far end never agreed to, and it would do so believing it had
    been told the whole shape.
    """
    return SchemaBudget(estimate=estimate, allowance=allowance)


def _tool(entry: dict[str, Any]) -> McpTool:
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise McpRefused("an advertised tool has a name")
    if len(name) > NAME_MAX_LENGTH:
        raise McpRefused(f"{name[:32]}… is longer than {NAME_MAX_LENGTH} characters")
    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        raise McpRefused(f"{name}: a description is text")
    declared = entry.get("inputSchema", entry.get("input_schema"))
    if declared is not None and not isinstance(declared, dict):
        raise McpRefused(f"{name}: an input schema is an object")
    # A tool that takes nothing still has a shape, and saying so explicitly is
    # what stops a model from inventing arguments for it.
    empty: dict[str, Any] = {"type": "object", "properties": {}}
    schema = empty if declared is None else cast(dict[str, Any], declared)
    return McpTool(name=name, description=description, input_schema=schema)


def _hash(tools: Sequence[McpTool]) -> str:
    return hashlib.sha256(
        json.dumps(
            [tool.content_hash for tool in tools],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
