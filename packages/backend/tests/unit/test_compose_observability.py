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
    # A labelled family with no child yet has no samples to collect at all.
    suffixes = {"histogram": ("_bucket", "_count", "_sum"), "counter": ("_total",)}
    names.update(
        f"{family.name}{suffix}"
        for family in REGISTRY.collect()
        for suffix in suffixes.get(family.type, ())
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


def _rules() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((OBSERVABILITY / "alerts.yml").read_text(encoding="utf-8"))
    return {
        rule["alert"]: rule
        for group in document["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }


def test_housekeeping_scans_are_not_held_to_the_every_pass_deadline() -> None:
    """2026-10-24: the housekeeping scans moved to once a minute. Under the
    60-second staleness rule every scan answers to, each would go stale
    between runs and page someone for a scheduler that is fine."""
    from tiny_hermes.runs.application.scheduler import HOUSEKEEPING_SCANS

    rules = _rules()
    every_pass = rules["SchedulerScanStalled"]["expr"]
    housekeeping = rules["SchedulerHousekeepingStalled"]["expr"]

    for scan in HOUSEKEEPING_SCANS:
        assert scan in every_pass, f"{scan} is still held to the every-pass deadline"
        assert scan in housekeeping, f"{scan} has no staleness rule of its own"
    assert 'scan!~"' in every_pass
    assert 'scan=~"' in housekeeping
    deadline = int(re.findall(r">\s*(\d+)", housekeeping)[-1])
    assert deadline >= 3 * 60, "three missed minutes, not one late pass"


def test_every_alert_has_a_promtool_test() -> None:
    """An alert nobody has seen fire is a guess about what will. The
    2026-10-24 drill found one that could not fire at all; `alerts.test.yml`
    shows each rule firing on its condition, and CI runs it with promtool."""
    tests = yaml.safe_load((OBSERVABILITY / "alerts.test.yml").read_text(encoding="utf-8"))
    tested = {
        case["alertname"]
        for group in tests["tests"]
        for case in group.get("alert_rule_test", [])
        if case.get("exp_alerts")
    }

    assert set(_rules()) <= tested, set(_rules()) - tested
