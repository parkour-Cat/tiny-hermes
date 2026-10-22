"""The `observability` profile (observability design §4, §5.4, §5.5).

What these pin: the metrics port is never published; the monitoring stack
starts only when asked for and listens on loopback; Prometheus scrapes every
process with the token; and every metric a rule or a panel names is one the
code actually registers. That last one is this project's most common bug in
small: a panel over a name nobody writes is a flat line that looks like calm.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import tiny_hermes.shared.metrics  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every metric
import yaml
from prometheus_client import REGISTRY

ROOT = Path(__file__).resolve().parents[4]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"
OBSERVABILITY = ROOT / "deploy" / "compose" / "observability"
APP_SERVICES = ("api", "worker", "scheduler", "controller", "egress-proxy")
MONITORING = ("prometheus", "grafana", "jaeger", "postgres-exporter", "redis-exporter")
METRICS_PORT = "9464"


def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def prometheus() -> dict[str, Any]:
    return yaml.safe_load((OBSERVABILITY / "prometheus.yml").read_text(encoding="utf-8"))


def test_no_service_publishes_the_metrics_port() -> None:
    for name, service in compose()["services"].items():
        for entry in service.get("ports", []):
            assert METRICS_PORT not in str(entry), name


@pytest.mark.parametrize("name", MONITORING)
def test_the_monitoring_stack_starts_only_when_asked_for(name: str) -> None:
    """A plain `docker compose up` stays what it was."""
    assert compose()["services"][name]["profiles"] == ["observability"]


@pytest.mark.parametrize("name", MONITORING)
def test_what_the_monitoring_stack_publishes_is_on_loopback(name: str) -> None:
    """Grafana ships with admin/admin and Jaeger with no login at all."""
    for entry in compose()["services"][name].get("ports", []):
        assert str(entry).startswith("127.0.0.1:"), (name, entry)


@pytest.mark.parametrize("name", APP_SERVICES)
def test_every_process_gets_the_metrics_token(name: str) -> None:
    environment = compose()["services"][name]["environment"]
    assert "METRICS_TOKEN" in environment, name


def test_prometheus_scrapes_every_process_with_the_token() -> None:
    [job] = [job for job in prometheus()["scrape_configs"] if job["job_name"] == "tiny-hermes"]
    [dns] = job["dns_sd_configs"]
    assert sorted(dns["names"]) == sorted(APP_SERVICES)
    assert str(dns["port"]) == METRICS_PORT
    assert job["authorization"]["credentials_file"]


def _named_metrics(text: str) -> set[str]:
    return set(re.findall(r"\btiny_hermes_[a-z_]+", text))


def _registered() -> set[str]:
    names: set[str] = set()
    for family in REGISTRY.collect():
        names.add(family.name)
        names.update(sample.name for sample in family.samples)
    # A histogram with no observation yet has no `_bucket` sample to collect.
    names.update(
        f"{family.name}{suffix}"
        for family in REGISTRY.collect()
        if family.type == "histogram"
        for suffix in ("_bucket", "_count", "_sum")
    )
    return names


def test_every_metric_an_alert_names_is_registered() -> None:
    named = _named_metrics((OBSERVABILITY / "alerts.yml").read_text(encoding="utf-8"))
    assert named
    assert named <= _registered(), named - _registered()


def test_every_metric_a_dashboard_panel_names_is_registered() -> None:
    dashboards = list((OBSERVABILITY / "grafana" / "dashboards").glob("*.json"))
    assert dashboards
    for dashboard in dashboards:
        expressions = [
            target["expr"]
            for panel in json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
            for target in panel.get("targets", [])
        ]
        named = _named_metrics("\n".join(expressions))
        assert named
        assert named <= _registered(), (dashboard.name, named - _registered())


def test_prometheus_loads_the_alert_rules() -> None:
    assert prometheus()["rule_files"] == ["/etc/prometheus/alerts.yml"]
    [mount] = [
        volume
        for volume in compose()["services"]["prometheus"]["volumes"]
        if str(volume).endswith(":/etc/prometheus/alerts.yml:ro")
    ]
    assert mount.startswith("./observability/alerts.yml:")
