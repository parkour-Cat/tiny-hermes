"""An endpoint says whether it accepts image input.

Declared per endpoint, never inferred from the model name. §7.4.2 already
requires that of `context_window` and `context_accounting`, and the reason
is sharper here: DeepSeek's vision support is a **different model id** from
its text one — `deepseek-v4-flash-vision-exp` beside `deepseek-v4-flash` —
so a check that looked for "vision" in a name would be a guess, and one
that goes silently wrong the next time a vendor renames anything.

`false` by default, which is not a cautious placeholder but the truth about
every endpoint registered before this column existed.

Getting it wrong in either direction is visible and safe. Declared when the
endpoint cannot: the provider refuses and the Run fails with the endpoint's
own status. Not declared when it could: images are refused by this platform
with a reason an administrator can act on. Neither silently sends a person
a wrong answer, which is what a name-sniffing guess would do on the day the
guess stopped matching.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0046"
down_revision: str | None = "20260825_0045"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "model_endpoints",
        sa.Column(
            "accepts_images",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_endpoints", "accepts_images")
