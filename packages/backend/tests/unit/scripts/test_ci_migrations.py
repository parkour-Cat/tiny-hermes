"""CI must round-trip the migrations this phase added, not jump over them."""

from pathlib import Path

CI = Path(__file__).resolve().parents[5] / ".github" / "workflows" / "ci.yml"


def test_ci_round_trips_the_4a_and_4c_migrations() -> None:
    text = CI.read_text(encoding="utf-8")
    check_at = text.index("uv run alembic check")
    chain = text[check_at:]
    assert "alembic downgrade 20260813_0010" in chain
    assert "alembic downgrade 20260813_0009" in chain
    assert chain.index("20260813_0010") < chain.index("20260813_0009")
    assert chain.index("20260813_0009") < chain.index("20260811_0008")
