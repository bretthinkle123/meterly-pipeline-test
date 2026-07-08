"""AC25 (IaC): the dashboard-reader Secrets Manager secret is CMK-encrypted
and the ECS task role's grant is a single resource-scoped `GetSecretValue`
statement with no wildcard `Action`/`Resource` — a static parse of the
Terraform source (no `terraform plan`/apply; no AWS backend in this sandboxed
test environment), mirroring the existing SLO static-assertion test."""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_TF = _REPO_ROOT / "infra" / "modules" / "data" / "main.tf"
_DATA_OUTPUTS_TF = _REPO_ROOT / "infra" / "modules" / "data" / "outputs.tf"
_COMPUTE_TF = _REPO_ROOT / "infra" / "modules" / "compute" / "main.tf"
_COMPUTE_VARS_TF = _REPO_ROOT / "infra" / "modules" / "compute" / "variables.tf"
_ROOT_TF = _REPO_ROOT / "infra" / "main.tf"


def _resource_block(text: str, resource_type: str, resource_name: str) -> str:
    match = re.search(
        rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(resource_name)}"\s*\{{(.*?)\n\}}',
        text,
        flags=re.DOTALL,
    )
    assert match is not None, f"expected resource {resource_type}.{resource_name} to exist"
    return match.group(1)


def test_dashboard_reader_secret_is_cmk_encrypted():
    text = _DATA_TF.read_text(encoding="utf-8")
    body = _resource_block(text, "aws_secretsmanager_secret", "dashboard_reader")
    assert re.search(r"kms_key_id\s*=\s*aws_kms_key\.data\.arn", body), (
        "dashboard_reader secret must be encrypted with the existing data CMK"
    )


def test_dashboard_reader_secret_value_is_out_of_band_not_a_real_plaintext():
    """The `secret_version` shell holds a placeholder with `ignore_changes`,
    never a real credential in Terraform source/state."""
    text = _DATA_TF.read_text(encoding="utf-8")
    body = _resource_block(text, "aws_secretsmanager_secret_version", "dashboard_reader")
    assert "ignore_changes" in body and "secret_string" in body
    assert "mtr_live" not in body, "no real API key literal may appear in Terraform source"


def test_dashboard_reader_secret_arn_is_exported():
    text = _DATA_OUTPUTS_TF.read_text(encoding="utf-8")
    assert re.search(
        r'output\s+"dashboard_reader_secret_arn"\s*\{[^}]*value\s*=\s*aws_secretsmanager_secret\.dashboard_reader\.arn',
        text,
        flags=re.DOTALL,
    ), "expected dashboard_reader_secret_arn output wired to the secret's arn"


def test_task_role_grant_is_resource_scoped_no_wildcard():
    """The `ReadDashboardReaderSecret` statement grants exactly
    `secretsmanager:GetSecretValue` on the dashboard-reader ARN variable —
    never `*` for Action or Resource (E-D1)."""
    text = _COMPUTE_TF.read_text(encoding="utf-8")
    body = _resource_block(text, "aws_iam_role_policy", "task")

    statement_match = re.search(
        r'Sid\s*=\s*"ReadDashboardReaderSecret".*?Action\s*=\s*(\[[^\]]*\]).*?Resource\s*=\s*([^\n,]+)',
        body,
        flags=re.DOTALL,
    )
    assert statement_match is not None, "expected a ReadDashboardReaderSecret IAM statement"
    action_literal, resource_literal = statement_match.groups()

    assert "secretsmanager:GetSecretValue" in action_literal
    assert "*" not in action_literal, f"Action must not contain a wildcard: {action_literal}"
    assert resource_literal.strip() == "var.dashboard_reader_secret_arn"
    assert "*" not in resource_literal, f"Resource must not be a wildcard: {resource_literal}"


def test_no_new_kms_grant_needed_reuses_existing_decrypt_statement():
    """Encrypting with the existing CMK means no new `kms:*` statement is
    required — only one `DecryptDataKey` statement should exist."""
    text = _COMPUTE_TF.read_text(encoding="utf-8")
    body = _resource_block(text, "aws_iam_role_policy", "task")
    kms_statement_count = len(re.findall(r'"kms:Decrypt"', body))
    assert kms_statement_count == 1, "expected exactly the pre-existing single kms:Decrypt grant, no new one"


def test_compute_module_declares_the_arn_input_variable():
    text = _COMPUTE_VARS_TF.read_text(encoding="utf-8")
    assert re.search(r'variable\s+"dashboard_reader_secret_arn"\s*\{', text)


def test_root_module_wires_data_output_into_compute_input():
    text = _ROOT_TF.read_text(encoding="utf-8")
    assert re.search(
        r"dashboard_reader_secret_arn\s*=\s*module\.data\.dashboard_reader_secret_arn", text
    ), "root main.tf must wire the data module's output into the compute module's input"
