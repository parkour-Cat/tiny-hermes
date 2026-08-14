"""4D acceptance records must exist and must not silently drop a §27.1 row."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
FEISHU = ROOT / "docs" / "superpowers" / "verification" / "2026-08-13-m1-feishu.md"
CLOSURE = ROOT / "docs" / "superpowers" / "verification" / "2026-08-13-m1-product-closure.md"
PYPROJECT = ROOT / "pyproject.toml"


def test_feishu_note_has_measured_unconfirmed_and_webhook_columns() -> None:
    text = FEISHU.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith("| Topic"))
    lowered = header.lower()
    assert "measured" in lowered
    assert "unconfirmed" in lowered
    assert "webhook-fallback" in lowered
    assert "adapter" in text.lower()
    assert "not a product" in text.lower()


def test_product_closure_record_has_thirteen_scenarios() -> None:
    text = CLOSURE.read_text(encoding="utf-8")
    numbered = [line for line in text.splitlines() if line.startswith("| ") and line[2].isdigit()]
    indexes = [int(line.split("|", 2)[1].strip()) for line in numbered]
    assert indexes == list(range(1, 14))
    assert "scripts/benchmark_m1.py" in text
    assert "2026-08-13-m1-feishu.md" in text
    assert "2026-08-14-m1-benchmark-live.md" in text
    assert "2026-08-14-m1-benchmark-live-2.md" in text


def test_public_description_is_the_m1_phrase() -> None:
    assert 'description = "单 Agent 安全运行骨架"' in PYPROJECT.read_text(encoding="utf-8")
