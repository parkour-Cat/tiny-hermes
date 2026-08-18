"""A bound operation, as the model hears about it and as it comes back.

§16.2's two steps for a family of tools rather than a fixed list. The first is
what the model is told; the second is what runs, and it is checked against the
*operation* rather than against the name the model typed.

The refusals are where the interesting decisions are. An argument the operation
never declared is refused rather than passed on, because a request carrying a
parameter nobody described is a request nobody reviewed. A path value is
percent-encoded including `/`, because a value that could add a segment would
let an argument pick a different endpoint than the operation names.
"""

import json
from uuid import uuid4

import pytest
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.tools.domain.http_calls import (
    BoundOperation,
    HttpCallRefused,
    http_call_of,
    schemas_for_operations,
)
from tiny_hermes.tools.domain.openapi import Operation, OperationParameter
from tiny_hermes.tools.domain.registry import schemas_for_agent

BASE = "https://api.example.com/v2"


def parameter(
    name: str, location: str = "query", required: bool = False
) -> OperationParameter:
    return OperationParameter(
        name=name,
        location=location,  # type: ignore[arg-type]
        required=required,
        schema={"type": "string"},
        description=f"the {name}",
    )


def bound(
    operation_id: str = "listOrders",
    method: str = "GET",
    path: str = "/orders",
    parameters: tuple[OperationParameter, ...] = (),
    body_schema: dict[str, object] | None = None,
    tool_name: str = "orders",
) -> BoundOperation:
    return BoundOperation(
        tool_name=tool_name,
        version_id=uuid4(),
        base_url=BASE,
        credential_ref="ORDERS_KEY",
        operation=Operation(
            operation_id=operation_id,
            method=method,
            path=path,
            summary=f"{method} {path}",
            parameters=parameters,
            body_schema=body_schema,
        ),
    )


def call(name: str, **arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="http-1", name=name, arguments=arguments)


# -- what the model is told --------------------------------------------------


def test_a_bound_operation_becomes_one_tool_the_model_can_name() -> None:
    schemas = schemas_for_operations([bound()])

    assert [schema["function"]["name"] for schema in schemas] == [
        "http.orders.listOrders"
    ]


def test_only_the_parameters_this_platform_sends_are_described() -> None:
    schemas = schemas_for_operations(
        [bound(parameters=(parameter("status"), parameter("limit")))]
    )

    properties = schemas[0]["function"]["parameters"]["properties"]
    assert set(properties) == {"status", "limit"}
    assert properties["status"]["description"] == "the status"


def test_a_required_parameter_is_required_and_an_optional_one_is_not() -> None:
    schemas = schemas_for_operations(
        [
            bound(
                path="/orders/{orderId}",
                parameters=(parameter("orderId", "path", True), parameter("status")),
            )
        ]
    )

    assert schemas[0]["function"]["parameters"]["required"] == ["orderId"]


def test_a_body_schema_is_offered_as_one_argument_called_body() -> None:
    schemas = schemas_for_operations(
        [bound(method="POST", body_schema={"type": "object"})]
    )

    parameters = schemas[0]["function"]["parameters"]
    assert parameters["properties"]["body"] == {"type": "object"}
    assert "body" in parameters["required"]


def test_a_write_says_so_in_its_description() -> None:
    """The model is told which of its tools change something, because the one
    that does will stop and wait for a person."""
    schemas = schemas_for_operations([bound(method="POST", operation_id="createOrder")])

    assert "changes data" in schemas[0]["function"]["description"]


def test_nothing_bound_is_nothing_described() -> None:
    assert schemas_for_operations([]) == []


# -- what actually runs ------------------------------------------------------


def test_a_bound_call_becomes_a_request_under_the_registered_base() -> None:
    plan = http_call_of(call("http.orders.listOrders"), [bound()])

    assert plan.method == "GET"
    assert plan.url == "https://api.example.com/v2/orders"
    assert plan.body is None
    assert plan.read_only is True


def test_the_base_url_s_own_prefix_survives() -> None:
    """`urljoin` would throw `/v2` away, and the request would go to an
    endpoint the registration never named."""
    plan = http_call_of(call("http.orders.listOrders"), [bound()])

    assert plan.url.startswith("https://api.example.com/v2/")


def test_a_path_parameter_is_substituted_and_encoded() -> None:
    plan = http_call_of(
        call("http.orders.readOrder", orderId="a b/c"),
        [
            bound(
                operation_id="readOrder",
                path="/orders/{orderId}",
                parameters=(parameter("orderId", "path", True),),
            )
        ],
    )

    # `/` encoded with everything else: a value that could add a segment would
    # let an argument choose a different endpoint than the operation names.
    assert plan.url == "https://api.example.com/v2/orders/a%20b%2Fc"


def test_query_parameters_are_encoded_into_the_url() -> None:
    plan = http_call_of(
        call("http.orders.listOrders", status="open & closed"),
        [bound(parameters=(parameter("status"),))],
    )

    assert plan.url.endswith("/orders?status=open+%26+closed")


def test_a_body_is_sent_as_json_with_the_header_that_says_so() -> None:
    plan = http_call_of(
        call("http.orders.createOrder", body={"sku": "abc"}),
        [
            bound(
                operation_id="createOrder",
                method="POST",
                body_schema={"type": "object"},
            )
        ],
    )

    assert plan.body is not None
    assert json.loads(plan.body) == {"sku": "abc"}
    assert plan.headers["Content-Type"] == "application/json"
    assert plan.read_only is False


def test_the_plan_carries_no_credential() -> None:
    """It is added where the request is sent. Nothing that passes through the
    model's side of the platform has ever held one."""
    plan = http_call_of(call("http.orders.listOrders"), [bound()])

    assert all("auth" not in key.lower() for key in plan.headers)


# -- and what is refused -----------------------------------------------------


def test_a_name_this_version_did_not_bind_is_not_authorized() -> None:
    with pytest.raises(HttpCallRefused) as refused:
        http_call_of(call("http.orders.deleteEverything"), [bound()])

    assert refused.value.reason == "tool_not_authorized"


def test_an_argument_the_operation_never_declared_is_refused() -> None:
    """Passed on, it would be a request carrying a parameter nobody described
    — and nobody reviewed."""
    with pytest.raises(HttpCallRefused) as refused:
        http_call_of(call("http.orders.listOrders", admin="true"), [bound()])

    assert refused.value.reason == "invalid_arguments"
    assert "admin" in refused.value.detail


def test_a_missing_required_parameter_is_refused_and_named() -> None:
    with pytest.raises(HttpCallRefused) as refused:
        http_call_of(
            call("http.orders.readOrder"),
            [
                bound(
                    operation_id="readOrder",
                    path="/orders/{orderId}",
                    parameters=(parameter("orderId", "path", True),),
                )
            ],
        )

    assert "orderId" in refused.value.detail


@pytest.mark.parametrize("value", [{"a": 1}, ["a"], None])
def test_an_argument_with_no_obvious_spelling_is_refused(value: object) -> None:
    """A dict in a query string is an encoding this platform would have to
    invent, and an invented encoding is one the far end never agreed to."""
    with pytest.raises(HttpCallRefused):
        http_call_of(
            call("http.orders.listOrders", status=value),
            [bound(parameters=(parameter("status"),))],
        )


def test_a_body_sent_to_an_operation_that_takes_none_is_refused() -> None:
    with pytest.raises(HttpCallRefused):
        http_call_of(call("http.orders.listOrders", body={"a": 1}), [bound()])


def test_an_operation_that_needs_a_body_and_gets_none_is_refused() -> None:
    with pytest.raises(HttpCallRefused):
        http_call_of(
            call("http.orders.createOrder"),
            [
                bound(
                    operation_id="createOrder",
                    method="POST",
                    body_schema={"type": "object"},
                )
            ],
        )


def test_two_tools_may_offer_the_same_operation_id_without_colliding() -> None:
    """The tool's own name is part of the call name, so two registrations of
    the same vendor API do not fight over `listOrders`."""
    first = bound(tool_name="orders")
    second = bound(tool_name="orders-staging")

    names = [
        schema["function"]["name"] for schema in schemas_for_operations([first, second])
    ]

    assert names == ["http.orders.listOrders", "http.orders-staging.listOrders"]
    plan = http_call_of(call("http.orders-staging.listOrders"), [first, second])
    assert plan.url.startswith(BASE)


def test_one_list_carries_the_named_tools_and_the_bound_operations() -> None:
    """The planner measures a schema list against the window and the request
    carries it; two functions producing two lists would be a round whose plan
    was about a different request."""
    names = [
        schema["function"]["name"]
        for schema in schemas_for_agent(("shell.exec",), [bound()])
    ]

    assert names == ["shell.exec", "http.orders.listOrders"]
