from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin

ENDPOINT_KINDS = ("openai_compatible",)
ENDPOINT_STATUSES = ("active", "disabled")
USAGE_QUALITIES = ("provider", "unavailable")
CONTEXT_ACCOUNTINGS = ("shared", "separate")


def _in_values(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


class ModelEndpointRow(IdMixin, CreatedAtMixin, Base):
    """A model endpoint a platform administrator approved.

    No ``workspace_id``: approval is a platform decision and a workspace only
    chooses among approved endpoints. No credential either — ``credential_ref``
    names an environment variable the deployment supplies, and the value is read
    at call time and stored nowhere.

    ``usage_quality`` is constrained to two values. ``estimated`` is excluded at
    the database level as well as in the schema, because a value that cannot be
    produced honestly should not be storable by any path.
    """

    __tablename__ = "model_endpoints"
    __table_args__ = (
        UniqueConstraint("name", name="uq_model_endpoints_name"),
        CheckConstraint(_in_values("kind", ENDPOINT_KINDS), name="ck_model_endpoints_kind"),
        CheckConstraint(
            _in_values("status", ENDPOINT_STATUSES), name="ck_model_endpoints_status"
        ),
        CheckConstraint(
            _in_values("usage_quality", USAGE_QUALITIES), name="ck_model_endpoints_usage"
        ),
        CheckConstraint(
            _in_values("context_accounting", CONTEXT_ACCOUNTINGS),
            name="ck_model_endpoints_context_accounting",
        ),
        CheckConstraint("context_window > 0", name="ck_model_endpoints_context_window"),
        CheckConstraint(
            "max_output_tokens > 0 AND max_output_tokens <= context_window",
            name="ck_model_endpoints_max_output",
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(2048))
    model: Mapped[str] = mapped_column(String(200))
    context_window: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    usage_quality: Mapped[str] = mapped_column(String(20))
    credential_ref: Mapped[str] = mapped_column(String(200))
    #: Defaulted rather than nullable: every endpoint registered before the
    #: budget planner existed has a window that behaves one way or the other,
    #: and `shared` is the reading that sends the smaller request.
    context_accounting: Mapped[str] = mapped_column(String(20), default="shared")
    #: Null means "no tokenizer declared", which is every row today. The
    #: planner's conservative bound is what answers then.
    tokenizer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[UUID] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
