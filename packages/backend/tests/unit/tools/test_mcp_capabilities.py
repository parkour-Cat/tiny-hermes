"""What an MCP server's advertisement is allowed to turn into.

The pure half of §16.2 for MCP. A `tools/list` answer goes in; a bindable list
comes out, or a refusal naming what is wrong with it.

Two rules get most of the attention here because they are the two that make MCP
different from an OpenAPI document.

**There is no way to bind everything.** §16.2 forbids handing a model whatever
a server discovered, and the way to make that stick is to have no expression
for it. `bound_tools` takes names. A test asserts that a server advertising a
tool nobody named contributes nothing — not to the schema list, not to the
budget.

**A server's words are data.** Descriptions arrive from somebody else's process
and land in the model's context. They are reference material, the same as skill
text, and nothing here resolves or executes anything one says.
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest
from tiny_hermes.tools.domain.mcp import (
    MAX_CAPABILITY_BYTES,
    MAX_TOOLS,
    McpRefused,
    bound_tools,
    call_name,
    estimated_tokens,
    fits_schema_budget,
    parse_capabilities,
    schemas_for_tools,
)

SEARCH: dict[str, Any] = {
    "name": "search",
    "description": "Search the index.",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

DELETE: dict[str, Any] = {
    "name": "delete",
    "description": "Remove a document.",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
}


def listing(*tools: Mapping[str, Any], envelope: bool = False) -> str:
    body: dict[str, Any] = {"tools": [dict(tool) for tool in tools]}
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": body} if envelope else body)


# -- what a server may say ---------------------------------------------------


def test_a_listing_becomes_tools_with_their_own_schemas() -> None:
    capabilities = parse_capabilities(listing(SEARCH, DELETE))

    assert [tool.name for tool in capabilities.tools] == ["delete", "search"]
    assert capabilities.tool("search") is not None
    assert capabilities.tool("search").input_schema["required"] == ["query"]  # pyright: ignore[reportOptionalMemberAccess]


def test_a_json_rpc_envelope_is_read_as_well_as_a_bare_result() -> None:
    """A transport and a document are different things, and only the document
    is read here."""
    wrapped = parse_capabilities(listing(SEARCH, envelope=True))
    bare = parse_capabilities(listing(SEARCH))

    assert wrapped.content_hash == bare.content_hash


def test_the_server_s_own_schema_is_kept_rather_than_rewritten() -> None:
    """What the model is told and what the far end validates against must be
    one document; a rewrite is a place for them to differ."""
    capabilities = parse_capabilities(listing(SEARCH))

    assert capabilities.tools[0].input_schema == SEARCH["inputSchema"]


def test_a_tool_that_takes_nothing_still_gets_a_shape() -> None:
    """Said explicitly, so a model does not invent arguments for it."""
    capabilities = parse_capabilities(listing({"name": "ping"}))

    assert capabilities.tools[0].input_schema == {"type": "object", "properties": {}}


def test_the_order_of_the_listing_does_not_change_the_hash() -> None:
    first = parse_capabilities(listing(SEARCH, DELETE))
    second = parse_capabilities(listing(DELETE, SEARCH))

    assert first.content_hash == second.content_hash


def test_a_changed_schema_changes_that_tool_s_hash_and_not_the_others() -> None:
    """§16.2's revalidation asks about the bound subset, so a server adding or
    changing something nobody bound must not invalidate the binding."""
    before = parse_capabilities(listing(SEARCH, DELETE))
    widened: dict[str, Any] = {
        **DELETE,
        "inputSchema": {"type": "object", "properties": {}},
    }
    after = parse_capabilities(listing(SEARCH, widened))

    assert after.tool("search").content_hash == before.tool("search").content_hash  # pyright: ignore[reportOptionalMemberAccess]
    assert after.tool("delete").content_hash != before.tool("delete").content_hash  # pyright: ignore[reportOptionalMemberAccess]


# -- and what it may not -----------------------------------------------------


def test_a_tool_with_no_name_cannot_be_bound() -> None:
    """The name is the key a binding is written against."""
    with pytest.raises(McpRefused):
        parse_capabilities(listing({"description": "nameless"}))


def test_two_tools_with_one_name_are_refused() -> None:
    with pytest.raises(McpRefused):
        parse_capabilities(listing(SEARCH, SEARCH))


def test_a_schema_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(McpRefused):
        parse_capabilities(listing({"name": "search", "inputSchema": "a string"}))


def test_a_description_that_is_not_text_is_refused() -> None:
    with pytest.raises(McpRefused):
        parse_capabilities(listing({"name": "search", "description": {"a": 1}}))


def test_an_answer_that_is_not_json_is_refused() -> None:
    with pytest.raises(McpRefused):
        parse_capabilities("not json at all")


def test_an_answer_with_no_tools_array_is_refused() -> None:
    with pytest.raises(McpRefused):
        parse_capabilities(json.dumps({"result": {}}))


def test_more_tools_than_anyone_could_review_are_refused() -> None:
    """A server offering more than this is not one an administrator can
    review, and reviewing is the whole control."""
    many = [{"name": f"tool{index}"} for index in range(MAX_TOOLS + 1)]

    with pytest.raises(McpRefused):
        parse_capabilities(json.dumps({"tools": many}))


def test_an_answer_too_large_to_hold_is_refused_by_its_size() -> None:
    with pytest.raises(McpRefused) as refused:
        parse_capabilities("x" * (MAX_CAPABILITY_BYTES + 1))

    assert str(MAX_CAPABILITY_BYTES) in str(refused.value)


# -- the subset, which is the whole control ----------------------------------


def test_only_the_named_tools_are_bound() -> None:
    capabilities = parse_capabilities(listing(SEARCH, DELETE))

    assert [tool.name for tool in bound_tools(capabilities, ["search"])] == ["search"]


def test_a_tool_nobody_named_reaches_neither_the_model_nor_the_budget() -> None:
    """§16.2: a server's discovery is not a permission. This is the test that
    says the platform has no way to express one."""
    capabilities = parse_capabilities(listing(SEARCH, DELETE))
    bound = bound_tools(capabilities, ["search"])

    names = [schema["function"]["name"] for schema in schemas_for_tools("docs", bound)]

    assert names == ["mcp.docs.search"]
    assert estimated_tokens("docs", bound) < estimated_tokens(
        "docs", capabilities.tools
    )


def test_binding_nothing_is_nothing() -> None:
    capabilities = parse_capabilities(listing(SEARCH))

    assert bound_tools(capabilities, []) == ()
    assert estimated_tokens("docs", ()) == 0


def test_a_bound_name_the_server_dropped_is_left_out_rather_than_raising() -> None:
    """A Run that lost one of four tools is better off than one that cannot
    start, and calling the missing name is refused by the second check anyway."""
    capabilities = parse_capabilities(listing(SEARCH))

    assert [tool.name for tool in bound_tools(capabilities, ["search", "gone"])] == [
        "search"
    ]


def test_two_servers_may_offer_the_same_tool_name_without_colliding() -> None:
    assert call_name("docs", "search") != call_name("tickets", "search")


# -- the schema budget -------------------------------------------------------


def test_a_subset_inside_the_allowance_fits() -> None:
    capabilities = parse_capabilities(listing(SEARCH))
    estimate = estimated_tokens("docs", capabilities.tools)

    assert fits_schema_budget(estimate, estimate).fits


def test_a_subset_over_the_allowance_does_not() -> None:
    capabilities = parse_capabilities(listing(SEARCH))
    estimate = estimated_tokens("docs", capabilities.tools)

    budget = fits_schema_budget(estimate, estimate - 1)

    assert not budget.fits
    # Both numbers travel, because a refusal an author can act on is one with
    # the numbers in it.
    assert budget.estimate == estimate
    assert budget.allowance == estimate - 1


def test_the_estimate_is_the_one_the_openapi_side_uses() -> None:
    """One estimator, so "how big is this binding" has a single answer no
    matter which kind of tool is asking."""
    from tiny_hermes.tools.domain.openapi import estimated_tokens_of

    capabilities = parse_capabilities(listing(SEARCH))
    tool = capabilities.tools[0]

    assert estimated_tokens("docs", capabilities.tools) == estimated_tokens_of(
        [
            {
                "name": "mcp.docs.search",
                "description": tool.description,
                "schema": tool.input_schema,
            }
        ]
    )
