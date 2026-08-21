"""§6's usage half: a workspace's spend, and why it never collapses to one number.

`cost_quality` exists per Run (`BudgetSummary`) so a `provider` figure — read
from what the endpoint actually billed — is never confused with an
`estimated` one. A workspace-level rollup that summed both into a single
total would recreate exactly the confusion that field was added to prevent,
just one level up. These tests pin that `WorkspaceUsageSummary` cannot be
read as a single blended cost: the only place a cost figure appears is inside
a `WorkspaceUsageByQuality` entry, keyed by the quality it belongs to.
"""

from decimal import Decimal

from tiny_hermes.runs.domain.models import (
    USAGE_WINDOW,
    WorkspaceUsageByQuality,
    WorkspaceUsageSummary,
)


def _bucket(**overrides: object) -> WorkspaceUsageByQuality:
    values: dict[str, object] = {
        "cost_quality": "provider",
        "consumed_cost": Decimal("12.340000"),
        "cost_currency": "USD",
        "run_count": 3,
        "consumed_model_calls": 9,
        "consumed_tool_calls": 4,
        "consumed_tokens": 12_345,
        "consumed_execution_ms": 67_890,
    }
    values.update(overrides)
    return WorkspaceUsageByQuality(**values)  # type: ignore[arg-type]


def test_a_cost_is_never_reachable_without_its_quality() -> None:
    """The failure this task exists to prevent: a lone `consumed_cost` a
    caller could read without knowing whether it is a bill or a guess."""
    summary = WorkspaceUsageSummary(
        window=USAGE_WINDOW,
        by_cost_quality=(
            _bucket(cost_quality="provider", consumed_cost=Decimal("12.34")),
            _bucket(
                cost_quality="unknown",
                consumed_cost=None,
                cost_currency=None,
                run_count=1,
                consumed_model_calls=0,
                consumed_tool_calls=0,
                consumed_tokens=0,
                consumed_execution_ms=0,
            ),
        ),
        total_run_count=4,
        total_model_calls=9,
        total_tool_calls=4,
        total_tokens=12_345,
        total_execution_ms=67_890,
    )

    document = summary.document()

    # No top-level key can hold a cost figure: the only route to one is
    # through a specific bucket's own `cost_quality`.
    assert "consumed_cost" not in document
    assert "cost_currency" not in document
    top_level_cost_like_keys = {
        key for key in document if "cost" in key and key != "by_cost_quality"
    }
    assert top_level_cost_like_keys == set()
    quality_seen = {item["cost_quality"] for item in document["by_cost_quality"]}
    assert quality_seen == {"provider", "unknown"}


def test_an_unknown_bucket_reports_no_cost_never_a_zero() -> None:
    """Same rule as a single Run's `BudgetSummary.consumed_cost`: unpriced is
    not free, so it must not serialize as `0`."""
    bucket = _bucket(cost_quality="unknown", consumed_cost=None, cost_currency=None)

    document = bucket.document()

    assert document["consumed_cost"] is None
    assert document["cost_currency"] is None


def test_money_serializes_as_a_string_not_a_float() -> None:
    """A JSON number is a float on the way to a screen — the one place this
    platform is careful never to send money through one (mirrors
    `BudgetSummary.document`)."""
    bucket = _bucket(consumed_cost=Decimal("12.340000"))

    document = bucket.document()

    assert document["consumed_cost"] == "12.340000"
    assert isinstance(document["consumed_cost"], str)


def test_the_window_is_explicit_on_the_wire_not_implied() -> None:
    summary = WorkspaceUsageSummary(
        window=USAGE_WINDOW,
        by_cost_quality=(),
        total_run_count=0,
        total_model_calls=0,
        total_tool_calls=0,
        total_tokens=0,
        total_execution_ms=0,
    )

    assert summary.document()["window"] == USAGE_WINDOW
    assert USAGE_WINDOW == "all_time"


def test_non_monetary_totals_may_blend_across_quality_because_none_is_money() -> None:
    """The grand totals that do exist at the top level are exactly the
    counters that mean the same thing regardless of quality — a token count
    does not become less true for being unpriced."""
    summary = WorkspaceUsageSummary(
        window=USAGE_WINDOW,
        by_cost_quality=(
            _bucket(cost_quality="provider", run_count=2, consumed_tokens=100),
            _bucket(cost_quality="unknown", run_count=1, consumed_tokens=50, consumed_cost=None, cost_currency=None),
        ),
        total_run_count=3,
        total_model_calls=18,
        total_tool_calls=8,
        total_tokens=150,
        total_execution_ms=135_780,
    )

    document = summary.document()

    assert document["total_run_count"] == 3
    assert document["total_tokens"] == 150
