"""AC12 (AC-SLO): SLOs (99.9% availability, ingest p95 < 50 ms) must be
defined as code via CloudWatch burn-rate + canary alarms, and the prod
environment must resolve to the exact alarm names `deploy.yml`'s
`<PROD_ALARM_NAMES>` expects.

This is a static assertion over the Terraform source (not a `terraform
plan`/apply run -- no AWS credentials or backend are available in this
sandboxed test environment), parsing `infra/modules/observability/main.tf`
for the `aws_cloudwatch_metric_alarm` resources the plan names, and
`infra/envs/prod/main.tf` for the `name_prefix` that resolves them to their
final `meterly-prod-*` form.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OBSERVABILITY_TF = _REPO_ROOT / "infra" / "modules" / "observability" / "main.tf"
_PROD_ENV_TF = _REPO_ROOT / "infra" / "envs" / "prod" / "main.tf"

# The exact alarm name suffixes AC12/deploy.yml's <PROD_ALARM_NAMES> expects,
# each as `${var.name_prefix}-<suffix>` in the Terraform source.
_REQUIRED_ALARM_SUFFIXES = [
    "slo-availability-fastburn",
    "slo-ingest-p95-fastburn",
    "5xx-rate",
    "alb-p95-latency",
    "unhealthy-hosts",
]


def _observability_tf_text() -> str:
    assert _OBSERVABILITY_TF.exists(), f"expected {_OBSERVABILITY_TF} to exist"
    return _OBSERVABILITY_TF.read_text(encoding="utf-8")


def test_every_required_alarm_resource_is_defined_with_the_right_name_expression():
    """Each required alarm exists as an `aws_cloudwatch_metric_alarm` resource
    whose `alarm_name` is built from `var.name_prefix`, so it resolves
    correctly per-environment (prod/staging) rather than being hardcoded."""
    text = _observability_tf_text()

    alarm_blocks = re.findall(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(\w+)"\s*\{(.*?)\n\}',
        text,
        flags=re.DOTALL,
    )
    assert alarm_blocks, "no aws_cloudwatch_metric_alarm resources found in observability module"

    alarm_names_by_resource = {}
    for resource_name, body in alarm_blocks:
        match = re.search(r'alarm_name\s*=\s*"([^"]+)"', body)
        assert match is not None, f"resource {resource_name} has no alarm_name"
        alarm_names_by_resource[resource_name] = match.group(1)

    for suffix in _REQUIRED_ALARM_SUFFIXES:
        matching = [
            name for name in alarm_names_by_resource.values()
            if name == f"${{var.name_prefix}}-{suffix}"
        ]
        assert matching, (
            f"expected an alarm named '${{var.name_prefix}}-{suffix}' in "
            f"{_OBSERVABILITY_TF}, found: {sorted(alarm_names_by_resource.values())}"
        )


def test_availability_slo_alarm_targets_the_error_rate_metric():
    """The availability-fastburn alarm watches the 5xx error metric (the
    signal an availability SLO burns down on)."""
    text = _observability_tf_text()
    match = re.search(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"slo_availability_fastburn"\s*\{(.*?)\n\}',
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    assert "HTTPCode_Target_5XX_Count" in body
    assert re.search(r'comparison_operator\s*=\s*"GreaterThanThreshold"', body)


def test_ingest_p95_slo_alarm_enforces_the_fifty_millisecond_budget():
    """The ingest-p95-fastburn alarm watches p95 latency with a 50ms threshold
    (AC-SLO's declared ingest p95 < 50ms budget), not some other percentile/value."""
    text = _observability_tf_text()
    match = re.search(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"slo_ingest_p95_fastburn"\s*\{(.*?)\n\}',
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    assert re.search(r'extended_statistic\s*=\s*"p95"', body), "must watch the p95 extended statistic, not an average"
    threshold_match = re.search(r'threshold\s*=\s*([\d.]+)', body)
    assert threshold_match is not None
    assert float(threshold_match.group(1)) == 0.05, "the ingest p95 SLO budget is 50ms (0.05s)"


def test_prod_environment_resolves_alarm_names_to_the_deploy_yml_expected_values():
    """`infra/envs/prod/main.tf` sets `name_prefix = "meterly-prod"`, so the
    resolved alarm names match exactly what deploy.yml's <PROD_ALARM_NAMES>
    lists: meterly-prod-slo-availability-fastburn,
    meterly-prod-slo-ingest-p95-fastburn, meterly-prod-5xx-rate,
    meterly-prod-alb-p95-latency, meterly-prod-unhealthy-hosts."""
    assert _PROD_ENV_TF.exists(), f"expected {_PROD_ENV_TF} to exist"
    prod_text = _PROD_ENV_TF.read_text(encoding="utf-8")

    match = re.search(r'name_prefix\s*=\s*"([^"]+)"', prod_text)
    assert match is not None, "prod env must set a literal name_prefix"
    name_prefix = match.group(1)
    assert name_prefix == "meterly-prod"

    expected_alarm_names = {f"{name_prefix}-{suffix}" for suffix in _REQUIRED_ALARM_SUFFIXES}
    assert expected_alarm_names == {
        "meterly-prod-slo-availability-fastburn",
        "meterly-prod-slo-ingest-p95-fastburn",
        "meterly-prod-5xx-rate",
        "meterly-prod-alb-p95-latency",
        "meterly-prod-unhealthy-hosts",
    }
