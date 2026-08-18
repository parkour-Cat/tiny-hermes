"""What each endpoint charges, as versions rather than as a current value.

Product design §12.4: a Run fixes the pricing version it was created under, so
an administrator correcting a price does not silently rewrite what yesterday's
Runs cost. That is only possible if the old price is still there, which is why
this is a version table and not three columns on the endpoint.

**A row here is a price. No row is not a price of zero.** The distinction lives
in `model_catalog/domain/pricing.py` and this table is what makes it storable:
an endpoint an administrator priced at zero has a row saying so, and one nobody
priced has none.

Money is `Numeric`, never a float. A float column would make two Runs that
spent the same amount disagree about whether they had.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin

REFERENCED_TABLE_MODULES = (identity_tables,)

#: Enough digits for a price quoted per million tokens to six decimal places,
#: and enough headroom that a long Run's total does not overflow the same type.
MONEY = Numeric(20, 6)


class ModelPricingVersionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "model_pricing_versions"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id", "version_number", name="uq_model_pricing_versions_number"
        ),
        CheckConstraint(
            "input_per_million >= 0 AND output_per_million >= 0 AND "
            "(cached_input_per_million IS NULL OR cached_input_per_million >= 0)",
            name="ck_model_pricing_versions_non_negative",
        ),
        CheckConstraint(
            "char_length(currency) = 3", name="ck_model_pricing_versions_currency"
        ),
        Index("ix_model_pricing_versions_endpoint", "endpoint_id", "effective_at"),
    )

    endpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_endpoints.id", ondelete="CASCADE")
    )
    version_number: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    input_per_million: Mapped[Decimal] = mapped_column(MONEY)
    output_per_million: Mapped[Decimal] = mapped_column(MONEY)
    #: Null means "the same as an ordinary input token" rather than "free".
    cached_input_per_million: Mapped[Decimal | None] = mapped_column(
        MONEY, nullable=True
    )
    #: When this price started applying. Recorded so a correction entered today
    #: can say it was true from last Monday, without pretending the Runs in
    #: between were priced under it — those fixed their own version.
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_model_pricing_versions_created_by"
        )
    )
