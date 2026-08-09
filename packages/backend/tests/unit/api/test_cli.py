import pytest
from tiny_hermes.api import cli


def test_cli_starts_uvicorn_on_public_container_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int]] = []

    def fake_run(app: str, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    cli.main()

    assert calls == [("tiny_hermes.api.app:app", "0.0.0.0", 8000)]
