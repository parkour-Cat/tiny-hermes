"""What a round cost, and what the platform is willing to say about it.

Product design §12.4. Everything here is pure arithmetic on `Decimal`, and the
whole module exists to keep one distinction from collapsing.

**Unknown is not zero.** An endpoint nobody has priced and an endpoint priced
at zero are different facts. The first means this platform cannot say what a
Run cost; the second means an administrator looked at a locally hosted model
and said it costs nothing. Conflating them would let a deployment believe it
was inside a spending limit that was never being enforced, which is the exact
failure a spending limit exists to prevent. So `Cost.amount` is `None` when
nothing can be said, and a currency ceiling that meets a `None` refuses to
pretend it was satisfied.

**Money is `Decimal`.** A float would make two Runs that spent the same amount
disagree about whether they had, and the disagreement would be invisible until
somebody added a column of them up.

**An estimate is labelled as one.** `CostQuality` travels with every amount, so
a number on a screen always carries how it was arrived at. §9.4's rule for
tokens applies to money for the same reason: a figure whose provenance is
unrecorded is one somebody will eventually treat as measured.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from tiny_hermes.runs.ports.model import UsageQuality

#: What a price is quoted per. A million rather than a thousand because that is
#: how every provider publishes, and a unit conversion nobody asked for is a
#: factor of a thousand waiting to be wrong.
TOKENS_PER_UNIT = Decimal(1_000_000)

#: How many decimal places an amount is kept to. Six is below the smallest
#: real-world charge and above the rounding error of a long Run — one round of
#: a cheap model costs fractions of a cent, and truncating those to two places
#: would make a thousand rounds cost nothing.
AMOUNT_PLACES = Decimal("0.000001")

#: The ISO 4217 length. Validated as a shape rather than against a list: this
#: platform does not convert currencies and has no business having opinions
#: about which ones exist.
CURRENCY_LENGTH = 3


class CostQuality(StrEnum):
    """How far an amount can be trusted, in the same spirit as `UsageQuality`.

    Deliberately the same three words a person already sees beside token
    counts. A second vocabulary for one idea would be a second thing to learn
    and a second place to get it wrong.
    """

    #: Counted from the provider's own usage report at a configured price.
    PROVIDER = "provider"
    #: Counted at a configured price from numbers this platform estimated. No
    #: path produces this yet — §9.4 admits an estimate only from a tokenizer
    #: verified against the model, and none ships. Named now because the field
    #: that carries it exists, and a value nobody can produce is safer than a
    #: reader assuming there are only two.
    ESTIMATED = "estimated"
    #: Nothing can be said: no price is configured, or the endpoint reported no
    #: usage. Never rendered as a zero.
    UNKNOWN = "unknown"


class InvalidPricing(Exception):
    """A price this platform will not store."""


@dataclass(frozen=True)
class TokenPrices:
    """What one endpoint charges, as an administrator entered it.

    A zero here is a real price. An endpoint with no `TokenPrices` at all is
    the unknown case, and the two are never the same object.
    """

    currency: str
    input_per_million: Decimal
    output_per_million: Decimal
    #: What a cached input token costs, when the provider prices them apart.
    #: `None` means "the same as an ordinary input token" rather than "free" —
    #: absence is not a discount.
    cached_input_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if len(self.currency) != CURRENCY_LENGTH or not self.currency.isalpha():
            raise InvalidPricing(f"{self.currency!r} is not a three-letter currency")
        if self.currency != self.currency.upper():
            raise InvalidPricing("a currency code is upper case")
        for name, value in (
            ("input_per_million", self.input_per_million),
            ("output_per_million", self.output_per_million),
            ("cached_input_per_million", self.cached_input_per_million),
        ):
            if value is None:
                continue
            if value < 0:
                # A negative price is not a discount this platform knows how to
                # apply; it is a typo that would make a Run appear to earn money.
                raise InvalidPricing(f"{name} cannot be negative")

    @property
    def is_free(self) -> bool:
        """Priced, and priced at nothing. Not the same as unpriced.

        Read as a word at call sites so the distinction the module exists for
        is spelled rather than inferred from two comparisons.
        """
        return self.input_per_million == 0 and self.output_per_million == 0


@dataclass(frozen=True)
class Cost:
    """An amount, its currency, and how it was arrived at.

    `amount` is `None` exactly when `quality` is `unknown`. Both are carried
    because a caller that only checked the amount would render `None` as zero,
    and a caller that only checked the quality would have nothing to show.
    """

    amount: Decimal | None
    currency: str | None
    quality: CostQuality

    @property
    def known(self) -> bool:
        return self.amount is not None

    def plus(self, other: "Cost") -> "Cost":
        """Two costs added, with the worse provenance of the two.

        An accumulated total is only as trustworthy as its least trustworthy
        part: one unpriced round makes the whole Run's cost unknown, because a
        total that silently omitted it would understate what was spent.
        """
        if not self.known or not other.known:
            return unknown()
        if self.currency != other.currency:
            # This platform does not convert. Two currencies in one total is a
            # number that means nothing, and inventing a rate would be worse.
            return unknown()
        mine, theirs = self.amount, other.amount
        if mine is None or theirs is None:  # pragma: no cover - `known` above
            return unknown()
        worse = (
            CostQuality.ESTIMATED
            if CostQuality.ESTIMATED in (self.quality, other.quality)
            else CostQuality.PROVIDER
        )
        return Cost(
            amount=_rounded(mine + theirs), currency=self.currency, quality=worse
        )


def unknown() -> Cost:
    """What this platform says when it cannot say. Never a zero."""
    return Cost(amount=None, currency=None, quality=CostQuality.UNKNOWN)


def free(currency: str) -> Cost:
    """Priced at nothing, which is a number and not an absence."""
    return Cost(amount=Decimal(0), currency=currency, quality=CostQuality.PROVIDER)


def cost_of(
    prices: TokenPrices | None,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    usage_quality: UsageQuality = UsageQuality.PROVIDER,
    cached_input_tokens: int | None = None,
) -> Cost:
    """What one round cost, or `unknown` when that cannot be answered.

    Three ways to be unknown and all of them produce the same answer: no price
    is configured, the endpoint declared it does not report usage, or it
    reported none. The alternative — treating a missing count as zero — is how
    a Run with no usable numbers appears to have been free.
    """
    if prices is None:
        return unknown()
    if usage_quality is UsageQuality.UNAVAILABLE:
        return unknown()
    if input_tokens is None and output_tokens is None:
        return unknown()
    cached = cached_input_tokens or 0
    plain = max((input_tokens or 0) - cached, 0)
    cached_rate = (
        prices.input_per_million
        if prices.cached_input_per_million is None
        else prices.cached_input_per_million
    )
    amount = (
        Decimal(plain) * prices.input_per_million
        + Decimal(cached) * cached_rate
        + Decimal(output_tokens or 0) * prices.output_per_million
    ) / TOKENS_PER_UNIT
    return Cost(
        amount=_rounded(amount),
        currency=prices.currency,
        quality=CostQuality.PROVIDER,
    )


def projected_cost(
    prices: TokenPrices | None, *, input_estimate: int, max_output_tokens: int
) -> Cost:
    """What the next round could cost at worst, for the pre-check.

    The largest output the endpoint may produce rather than a guess at the
    likely one: a valve that let a call through on an optimistic projection
    would be a valve that only stops the calls that were going to be cheap.
    """
    return cost_of(
        prices,
        input_tokens=input_estimate,
        output_tokens=max_output_tokens,
    )


@dataclass(frozen=True)
class CostCeiling:
    """The most a Run may spend, when a Run has such a limit.

    `None` is no ceiling rather than a ceiling of zero — the same distinction
    the whole module turns on, in the one place where getting it backwards
    would stop every Run instead of none.
    """

    max_amount: Decimal | None
    currency: str | None = None


@dataclass(frozen=True)
class CeilingVerdict:
    allowed: bool
    #: Why not, for a person reading a paused Run. Empty when allowed.
    reason: str = ""


def within_ceiling(
    ceiling: CostCeiling, consumed: Cost, projected: Cost
) -> CeilingVerdict:
    """Whether one more round fits under the ceiling.

    A ceiling that meets an unknown cost **refuses**. That is the decision this
    function exists to make, and the opposite reading is the dangerous one: a
    deployment with a spending limit and an unpriced endpoint would believe it
    was protected while nothing was being counted.

    A Run with no ceiling is allowed regardless — it never asked to be
    measured, so an unknown cost costs it nothing.
    """
    if ceiling.max_amount is None:
        return CeilingVerdict(allowed=True)
    if not consumed.known or not projected.known:
        return CeilingVerdict(
            allowed=False,
            reason=(
                "this Run has a spending limit and its endpoint has no price "
                "configured, so what it has spent cannot be counted"
            ),
        )
    if ceiling.currency is not None and ceiling.currency != projected.currency:
        return CeilingVerdict(
            allowed=False,
            reason=(
                f"the limit is in {ceiling.currency} and this endpoint charges "
                f"in {projected.currency}; this platform does not convert"
            ),
        )
    spent, more = consumed.amount, projected.amount
    if spent is None or more is None:  # pragma: no cover - `known` above
        return CeilingVerdict(allowed=False, reason="nothing can be counted")
    if spent + more > ceiling.max_amount:
        return CeilingVerdict(
            allowed=False,
            reason=(
                f"{spent} spent plus up to {more} more would pass the "
                f"limit of {ceiling.max_amount}"
            ),
        )
    return CeilingVerdict(allowed=True)


@dataclass(frozen=True)
class EnforcedValves:
    """Which of §12.3's valves are in force for this endpoint.

    §12.4: an endpoint that does not report usage cannot have its tokens or its
    money counted, so those two ceilings are disabled rather than enforced
    against numbers nobody has. The other three do not depend on the endpoint
    reporting anything and stay on — which is the half a reader is most likely
    to assume was disabled too, and the half that is doing the work.
    """

    tokens: bool
    currency: bool
    elapsed_time: bool
    model_calls: bool
    max_output_tokens: bool


def enforced_valves(usage_quality: UsageQuality) -> EnforcedValves:
    counted = usage_quality is not UsageQuality.UNAVAILABLE
    return EnforcedValves(
        tokens=counted,
        currency=counted,
        # These three are facts about the platform's own clock and counters, so
        # an endpoint that reports nothing does not weaken them.
        elapsed_time=True,
        model_calls=True,
        max_output_tokens=True,
    )


def _rounded(amount: Decimal) -> Decimal:
    return amount.quantize(AMOUNT_PLACES, rounding=ROUND_HALF_UP)
