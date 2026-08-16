from __future__ import annotations

import pytest

from src.guardrails.engine import PolicyEngine
from src.guardrails.redact import redact_fields, redact_value
from src.guardrails.result import PolicyDecision, RiskTier


@pytest.fixture
def engine():
    return PolicyEngine()


def test_safe_read_route_allowed(engine):
    result = engine.check_action(
        "navigate", "http://localhost:4478/desk/member/4521"
    )
    assert result.decision == PolicyDecision.ALLOW
    assert result.risk_tier == RiskTier.SAFE


def test_off_allowlist_origin_denied(engine):
    result = engine.check_action("navigate", "https://evil.example.com/desk")
    assert result.decision == PolicyDecision.DENY


def test_disallowed_action_type_denied(engine):
    result = engine.check_action(
        "execute_shell", "http://localhost:4478/desk/member/4521"
    )
    assert result.decision == PolicyDecision.DENY


def test_mutating_route_requires_confirmation_by_default(engine):
    result = engine.check_action(
        "click", "http://localhost:4478/desk/member/4521/subaccount/new"
    )
    assert result.decision == PolicyDecision.REQUIRE_CONFIRMATION
    assert result.risk_tier == RiskTier.MUTATING


def test_mutating_route_allowed_with_artifact_approved(engine):
    result = engine.check_action(
        "click",
        "http://localhost:4478/desk/member/4521/subaccount/new",
        artifact_approved=True,
    )
    assert result.decision == PolicyDecision.ALLOW


def test_mutating_route_allowed_with_explicit_confirmation(engine):
    result = engine.check_action(
        "click",
        "http://localhost:4478/desk/member/4521/subaccount/new",
        confirmed=True,
    )
    assert result.decision == PolicyDecision.ALLOW


def test_irreversible_route_requires_confirmation_even_if_artifact_approved(engine):
    result = engine.check_action(
        "click",
        "http://localhost:4478/desk/member/4521/subaccount/confirm",
        artifact_approved=True,
    )
    assert result.decision == PolicyDecision.REQUIRE_CONFIRMATION
    assert result.risk_tier == RiskTier.IRREVERSIBLE


def test_irreversible_route_can_never_be_authorized_by_a_flag(engine):
    """The safety property is structural, not flag-dependent: no combination
    of artifact approval and confirmation flags lets an irreversible action
    run unattended. It always routes to a human taking control of the live
    session (replay handles that escalation -- see src/replay/engine.py)."""
    for artifact_approved in (False, True):
        for confirmed in (False, True):
            result = engine.check_action(
                "click",
                "http://localhost:4478/desk/member/4521/subaccount/confirm",
                artifact_approved=artifact_approved,
                confirmed=confirmed,
            )
            assert result.decision == PolicyDecision.REQUIRE_CONFIRMATION, (
                "artifact_approved=" + str(artifact_approved) + " confirmed=" + str(confirmed)
            )
            assert result.risk_tier == RiskTier.IRREVERSIBLE


def test_tenant_b_origin_also_allowed(engine):
    result = engine.check_action(
        "navigate", "http://localhost:4479/operations/member/1002"
    )
    assert result.decision == PolicyDecision.ALLOW


def test_budget_step_limit(engine):
    result = engine.check_budget(step_count=999, elapsed_seconds=1)
    assert result.decision == PolicyDecision.DENY


def test_budget_duration_limit(engine):
    result = engine.check_budget(step_count=1, elapsed_seconds=99999)
    assert result.decision == PolicyDecision.DENY


def test_budget_within_limits(engine):
    result = engine.check_budget(step_count=5, elapsed_seconds=10)
    assert result.decision == PolicyDecision.ALLOW


def test_redact_value_fully_replaces_not_partially_masks():
    """Full replacement, not partial masking -- partial masking still
    leaks the value's length and first/last characters, which for a short
    numeric code or balance can narrow it down considerably."""
    assert redact_value("2468") == "***REDACTED***"
    assert redact_value("ab") == "***REDACTED***"
    assert redact_value("") == ""


def test_redact_fields_only_masks_sensitive_keys():
    data = {"member_id": "4521", "supervisor_code": "2468", "account_type": "Holiday Savings"}
    redacted = redact_fields(data, {"supervisor_code"})
    assert redacted["member_id"] == "4521"
    assert redacted["account_type"] == "Holiday Savings"
    assert redacted["supervisor_code"] == "***REDACTED***"
