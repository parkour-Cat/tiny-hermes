"""What a round cost, and the one distinction this whole module exists for.

**Unknown is not zero.** An endpoint nobody priced and an endpoint priced at
zero are different facts, and most of this file is one pair of tests after
another saying so: the same call, once with no price and once with a price of
zero, must not produce the same answer.

It matters because of `within_ceiling`. A deployment with a spending limit and
an unpriced endpoint must not believe it is protected — so a ceiling that meets
an unknown cost refuses, and that is asserted directly rather than left to
follow from the rest.

The last section pins §12.4's asymmetry: an endpoint that reports no usage
loses its token and money ceilings and keeps the other three. One test asserts
all five at once, because checking only the disabled half is how a reader comes
away believing such an endpoint is unlimited.
"""

from decimal import Decimal

import pytest
from tiny_hermes.model_catalog.domain.pricing import (
    CeilingVerdict,
    Cost,
    CostCeiling,
    CostQuality,
    InvalidPricing,
    TokenPrices,
    cost_of,
    enforced_valves,
    free,
    projected_cost,
    unknown,
    within_ceiling,
)
from tiny_hermes.runs.ports.model import UsageQuality

PAID = TokenPrices(
    currency="USD",
    input_per_million=Decimal("3"),
    output_per_million=Decimal("15"),
)

FREE = TokenPrices(
    currency="USD",
    input_per_million=Decimal("0"),
    output_per_million=Decimal("0"),
)


# -- what a price may be -----------------------------------------------------


def test_a_price_of_zero_is_a_price() -> None:
    """An administrator looked at a locally hosted model and said it costs
    nothing. That is a fact, not an absence of one."""
    assert FREE.is_free
    assert cost_of(FREE, input_tokens=1_000, output_tokens=1_000).known


def test_no_price_at_all_is_not_a_price_of_zero() -> None:
    """The pair. Same call, no configured price, and the answer is not zero."""
    priced = cost_of(FREE, input_tokens=1_000, output_tokens=1_000)
    unpriced = cost_of(None, input_tokens=1_000, output_tokens=1_000)

    assert priced.amount == Decimal(0)
    assert unpriced.amount is None
    assert unpriced.quality is CostQuality.UNKNOWN


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "US1"])
def test_a_currency_that_is_not_one_is_refused(currency: str) -> None:
    with pytest.raises(InvalidPricing):
        TokenPrices(
            currency=currency,
            input_per_million=Decimal("1"),
            output_per_million=Decimal("1"),
        )


def test_a_negative_price_is_refused() -> None:
    """Not a discount this platform knows how to apply; a typo that would make
    a Run appear to earn money."""
    with pytest.raises(InvalidPricing):
        TokenPrices(
            currency="USD",
            input_per_million=Decimal("-1"),
            output_per_million=Decimal("1"),
        )


# -- what one round cost -----------------------------------------------------


def test_a_round_is_priced_from_its_two_token_counts() -> None:
    cost = cost_of(PAID, input_tokens=1_000_000, output_tokens=1_000_000)

    assert cost.amount == Decimal("18")
    assert cost.currency == "USD"
    assert cost.quality is CostQuality.PROVIDER


def test_fractions_of_a_cent_survive() -> None:
    """One round of a cheap model costs fractions of a cent, and truncating
    those to two places would make a thousand rounds cost nothing."""
    cost = cost_of(PAID, input_tokens=100, output_tokens=100)

    assert cost.amount is not None
    assert cost.amount > 0


def test_cached_input_is_priced_apart_when_the_provider_prices_it_apart() -> None:
    prices = TokenPrices(
        currency="USD",
        input_per_million=Decimal("3"),
        output_per_million=Decimal("15"),
        cached_input_per_million=Decimal("0.3"),
    )

    cost = cost_of(
        prices,
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=1_000_000,
    )

    assert cost.amount == Decimal("0.3")


def test_absent_cached_pricing_is_not_a_discount() -> None:
    """`None` means "the same as an ordinary input token", not "free"."""
    cost = cost_of(
        PAID, input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000
    )

    assert cost.amount == Decimal("3")


def test_an_endpoint_that_reports_no_usage_costs_an_unknown_amount() -> None:
    cost = cost_of(
        PAID,
        input_tokens=None,
        output_tokens=None,
        usage_quality=UsageQuality.UNAVAILABLE,
    )

    assert cost.quality is CostQuality.UNKNOWN


def test_a_priced_endpoint_that_reported_nothing_is_still_unknown() -> None:
    """Treating a missing count as zero is how a Run with no usable numbers
    appears to have been free."""
    cost = cost_of(PAID, input_tokens=None, output_tokens=None)

    assert cost.amount is None


def test_a_round_that_used_nothing_but_reported_it_costs_zero() -> None:
    """Reported zero and reported nothing are different, and only one of them
    is an amount."""
    cost = cost_of(PAID, input_tokens=0, output_tokens=0)

    assert cost.amount == Decimal(0)
    assert cost.quality is CostQuality.PROVIDER


# -- adding them up ----------------------------------------------------------


def test_two_known_costs_add() -> None:
    total = free("USD").plus(cost_of(PAID, input_tokens=1_000_000, output_tokens=0))

    assert total.amount == Decimal("3")


def test_one_unknown_round_makes_the_whole_total_unknown() -> None:
    """A total that silently omitted it would understate what was spent."""
    total = cost_of(PAID, input_tokens=1_000, output_tokens=1_000).plus(unknown())

    assert total.amount is None
    assert total.quality is CostQuality.UNKNOWN


def test_two_currencies_do_not_add() -> None:
    """This platform does not convert, and inventing a rate would be worse than
    saying nothing."""
    euros = TokenPrices(
        currency="EUR",
        input_per_million=Decimal("3"),
        output_per_million=Decimal("15"),
    )

    total = free("USD").plus(cost_of(euros, input_tokens=1_000, output_tokens=0))

    assert total.quality is CostQuality.UNKNOWN


def test_an_estimate_anywhere_makes_the_total_an_estimate() -> None:
    estimated = Cost(
        amount=Decimal("1"), currency="USD", quality=CostQuality.ESTIMATED
    )

    total = free("USD").plus(estimated)

    assert total.quality is CostQuality.ESTIMATED


# -- the pre-check -----------------------------------------------------------


def test_a_projection_uses_the_largest_output_the_endpoint_may_produce() -> None:
    """A valve that let a call through on an optimistic projection would be a
    valve that only stops the calls that were going to be cheap."""
    projected = projected_cost(PAID, input_estimate=0, max_output_tokens=1_000_000)

    assert projected.amount == Decimal("15")


def test_a_projection_with_no_price_is_unknown() -> None:
    assert not projected_cost(None, input_estimate=10, max_output_tokens=10).known


# -- the ceiling -------------------------------------------------------------


def _ceiling(amount: str | None, currency: str | None = "USD") -> CostCeiling:
    return CostCeiling(
        max_amount=None if amount is None else Decimal(amount), currency=currency
    )


def test_a_round_that_fits_is_allowed() -> None:
    verdict = within_ceiling(_ceiling("10"), free("USD"), free("USD"))

    assert verdict.allowed


def test_a_round_that_would_pass_the_limit_is_refused_with_both_numbers() -> None:
    consumed = cost_of(PAID, input_tokens=1_000_000, output_tokens=0)
    projected = cost_of(PAID, input_tokens=1_000_000, output_tokens=0)

    verdict = within_ceiling(_ceiling("5"), consumed, projected)

    assert not verdict.allowed
    assert "5" in verdict.reason


def test_a_ceiling_that_meets_an_unknown_cost_refuses() -> None:
    """The decision this function exists to make. A deployment with a spending
    limit and an unpriced endpoint must not believe it is protected."""
    verdict = within_ceiling(_ceiling("10"), unknown(), unknown())

    assert not verdict.allowed
    assert "no price" in verdict.reason


def test_an_unknown_projection_alone_is_enough_to_refuse() -> None:
    verdict = within_ceiling(_ceiling("10"), free("USD"), unknown())

    assert not verdict.allowed


def test_a_run_with_no_ceiling_is_allowed_even_when_nothing_can_be_counted() -> None:
    """It never asked to be measured, so an unknown cost costs it nothing."""
    verdict = within_ceiling(_ceiling(None), unknown(), unknown())

    assert verdict.allowed


def test_a_ceiling_in_another_currency_refuses_rather_than_converting() -> None:
    euros = TokenPrices(
        currency="EUR",
        input_per_million=Decimal("3"),
        output_per_million=Decimal("15"),
    )

    verdict = within_ceiling(
        _ceiling("10", "USD"),
        free("EUR"),
        cost_of(euros, input_tokens=1_000, output_tokens=0),
    )

    assert not verdict.allowed
    assert "convert" in verdict.reason


def test_exactly_the_limit_is_allowed_and_a_penny_over_is_not() -> None:
    """The boundary decided here rather than left to a comparison somebody
    flips while refactoring."""
    at_limit = within_ceiling(
        _ceiling("3"), free("USD"), cost_of(PAID, input_tokens=1_000_000, output_tokens=0)
    )
    over = within_ceiling(
        _ceiling("2.999999"),
        free("USD"),
        cost_of(PAID, input_tokens=1_000_000, output_tokens=0),
    )

    assert at_limit == CeilingVerdict(allowed=True)
    assert not over.allowed


# -- §12.4's asymmetry -------------------------------------------------------


def test_an_endpoint_that_reports_usage_has_every_valve_in_force() -> None:
    valves = enforced_valves(UsageQuality.PROVIDER)

    assert valves.tokens
    assert valves.currency
    assert valves.elapsed_time
    assert valves.model_calls
    assert valves.max_output_tokens


def test_an_endpoint_that_reports_nothing_loses_two_valves_and_keeps_three() -> None:
    """All five asserted together on purpose. Checking only the disabled half
    is how a reader comes away believing such an endpoint is unlimited."""
    valves = enforced_valves(UsageQuality.UNAVAILABLE)

    assert not valves.tokens
    assert not valves.currency
    assert valves.elapsed_time
    assert valves.model_calls
    assert valves.max_output_tokens
