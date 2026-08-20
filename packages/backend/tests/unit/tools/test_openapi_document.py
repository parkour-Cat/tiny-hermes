"""What a description of somebody else's API is allowed to turn into.

Product design §16.1 puts OpenAPI-derived tools in M2. This is the pure half:
a document goes in, a list of operations an Agent may be bound to comes out, or
a refusal naming what is wrong with it. Nothing here opens a socket.

Two rules run through the file and are worth stating once.

**An operation without an `operationId` cannot be bound.** The id is the key a
binding is written against; a generated one would change when the document is
reordered, and a published Agent would silently start calling something else.

**Nothing is accepted that a person could not check.** A path template with a
traversal in it, a parameter in a place this platform does not implement, a
document with four hundred operations — each is refused rather than partly
supported, because the alternative is a tool list nobody reviews.
"""

import json

import pytest
from tiny_hermes.tools.domain.openapi import (
    MAX_DOCUMENT_BYTES,
    MAX_OPERATIONS,
    OpenApiRefused,
    estimated_schema_tokens,
    parse_document,
)

MINIMAL = {
    "openapi": "3.0.3",
    "info": {"title": "Orders", "version": "1"},
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "summary": "List every order.",
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Filter by status.",
                    }
                ],
            },
            "post": {
                "operationId": "createOrder",
                "summary": "Place an order.",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"sku": {"type": "string"}},
                                "required": ["sku"],
                            }
                        }
                    }
                },
            },
        },
        "/orders/{orderId}": {
            "get": {
                "operationId": "readOrder",
                "parameters": [
                    {
                        "name": "orderId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
    },
}


def document(**overrides: object) -> str:
    body = {**MINIMAL, **overrides}
    return json.dumps(body)


# -- what comes out ---------------------------------------------------------


def test_every_named_operation_becomes_one_bindable_entry() -> None:
    parsed = parse_document(document())

    assert [operation.operation_id for operation in parsed.operations] == [
        "createOrder",
        "listOrders",
        "readOrder",
    ]


def test_operations_come_back_in_a_stable_order() -> None:
    """Two reads of one document agree, so a content hash means something and a
    reviewer sees the same list twice."""
    first = parse_document(document())
    second = parse_document(document())

    assert [item.operation_id for item in first.operations] == [
        item.operation_id for item in second.operations
    ]


def test_an_operation_carries_what_a_caller_needs_to_make_the_request() -> None:
    parsed = parse_document(document())
    read = next(item for item in parsed.operations if item.operation_id == "readOrder")

    assert read.method == "GET"
    assert read.path == "/orders/{orderId}"
    assert [parameter.name for parameter in read.parameters] == ["orderId"]
    assert read.parameters[0].location == "path"
    assert read.parameters[0].required is True


def test_a_read_is_told_apart_from_a_write() -> None:
    """The distinction the whole first stage rests on: a write does not run
    until somebody approves it, and something has to decide which is which."""
    parsed = parse_document(document())
    listed = next(item for item in parsed.operations if item.operation_id == "listOrders")
    created = next(item for item in parsed.operations if item.operation_id == "createOrder")

    assert listed.read_only is True
    assert created.read_only is False


def test_a_request_body_schema_travels_with_the_operation() -> None:
    parsed = parse_document(document())
    created = next(item for item in parsed.operations if item.operation_id == "createOrder")

    assert created.body_schema == {
        "type": "object",
        "properties": {"sku": {"type": "string"}},
        "required": ["sku"],
    }


def test_the_same_document_hashes_the_same_whatever_its_spacing() -> None:
    """A binding names a version, and a version is its content — so two
    spellings of one document must not be two versions."""
    spaced = json.dumps(MINIMAL, indent=4)

    assert parse_document(document()).content_hash == parse_document(spaced).content_hash


# -- what is refused --------------------------------------------------------


def test_a_document_that_is_not_json_is_refused() -> None:
    """JSON only, and deliberately: a YAML parser is a large surface to point
    at a document somebody uploaded, and every OpenAPI tool can emit JSON. The
    same argument the skill manifest's tiny frontmatter parser makes."""
    with pytest.raises(OpenApiRefused):
        parse_document("openapi: 3.0.3\ninfo:\n  title: Orders\n")


def test_a_document_that_is_not_an_openapi_document_is_refused() -> None:
    for body in ('{"paths": {}}', '{"openapi": "3.0.3"}', "[]", '"a string"'):
        with pytest.raises(OpenApiRefused):
            parse_document(body)


def test_an_operation_without_an_id_is_refused_rather_than_named_by_us() -> None:
    """A generated id would move when the document is reordered, and a
    published Agent would quietly start calling something else."""
    with pytest.raises(OpenApiRefused) as refused:
        parse_document(
            document(paths={"/orders": {"get": {"summary": "no id here"}}})
        )

    assert "operationId" in str(refused.value)


def test_two_operations_with_one_id_are_refused() -> None:
    with pytest.raises(OpenApiRefused):
        parse_document(
            document(
                paths={
                    "/a": {"get": {"operationId": "same"}},
                    "/b": {"get": {"operationId": "same"}},
                }
            )
        )


@pytest.mark.parametrize(
    "path",
    ["orders", "/orders/../admin", "/orders//items", "/orders/{}", "/orders/{ id }"],
)
def test_a_path_nobody_could_check_is_refused(path: str) -> None:
    with pytest.raises(OpenApiRefused):
        parse_document(document(paths={path: {"get": {"operationId": "readOne"}}}))


def test_a_path_parameter_the_template_never_names_is_refused() -> None:
    """Otherwise a caller supplies a value that goes nowhere, and the request
    that is finally sent is not the one the model asked for."""
    with pytest.raises(OpenApiRefused):
        parse_document(
            document(
                paths={
                    "/orders/{orderId}": {
                        "get": {
                            "operationId": "readOrder",
                            "parameters": [
                                {
                                    "name": "somethingElse",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                        }
                    }
                }
            )
        )


def test_a_template_variable_no_parameter_fills_is_refused() -> None:
    with pytest.raises(OpenApiRefused):
        parse_document(
            document(paths={"/orders/{orderId}": {"get": {"operationId": "readOrder"}}})
        )


@pytest.mark.parametrize("location", ["header", "cookie"])
def test_a_parameter_this_platform_does_not_implement_is_refused(location: str) -> None:
    """Refused rather than ignored. A header parameter silently dropped is a
    request that goes out without the thing its author thought it carried —
    and headers are where credentials live, which is the platform's business
    and not a model's.
    """
    with pytest.raises(OpenApiRefused):
        parse_document(
            document(
                paths={
                    "/orders": {
                        "get": {
                            "operationId": "listOrders",
                            "parameters": [
                                {
                                    "name": "x-tenant",
                                    "in": location,
                                    "schema": {"type": "string"},
                                }
                            ],
                        }
                    }
                }
            )
        )


def test_a_document_larger_than_the_ceiling_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(OpenApiRefused) as refused:
        parse_document("x" * (MAX_DOCUMENT_BYTES + 1))

    # The size is in the refusal: an administrator holding a 4 MB export needs
    # to know it is the size rather than the syntax.
    assert str(MAX_DOCUMENT_BYTES) in str(refused.value)


def test_more_operations_than_anyone_would_review_are_refused() -> None:
    paths = {
        f"/thing{index}": {"get": {"operationId": f"read{index}"}}
        for index in range(MAX_OPERATIONS + 1)
    }

    with pytest.raises(OpenApiRefused):
        parse_document(document(paths=paths))


def test_a_method_this_platform_does_not_serve_is_left_out_rather_than_refused() -> None:
    """`trace` is not a refusal of the document: the rest of it is still
    usable, and refusing a whole API because one operation is exotic would
    push an administrator into editing somebody else's export."""
    parsed = parse_document(
        document(
            paths={
                "/orders": {
                    "get": {"operationId": "listOrders"},
                    "trace": {"operationId": "traceOrders"},
                }
            }
        )
    )

    assert [item.operation_id for item in parsed.operations] == ["listOrders"]


# -- what it costs ----------------------------------------------------------


def test_the_estimate_grows_with_the_schema_it_describes() -> None:
    """One function for this, used by the budget check and by the console, so
    the two can never disagree about how big a binding is."""
    parsed = parse_document(document())
    listed = next(item for item in parsed.operations if item.operation_id == "listOrders")
    created = next(item for item in parsed.operations if item.operation_id == "createOrder")

    assert estimated_schema_tokens([listed]) > 0
    assert estimated_schema_tokens([listed, created]) > estimated_schema_tokens([listed])


def test_nothing_bound_costs_nothing() -> None:
    assert estimated_schema_tokens([]) == 0
