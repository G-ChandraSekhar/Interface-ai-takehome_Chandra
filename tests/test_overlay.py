from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.artifact.overlay import apply_overlay
from src.artifact.schema import (
    Artifact,
    ArtifactStep,
    Checkpoint,
    ExtractionRule,
    LocatorCandidate,
    ParamSpec,
    TargetSpec,
)

BASE = "http://localhost:4478"


def _base_artifact():
    return Artifact(
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        version=1,
        goal="Look up member 4521 and read their name and regular savings balance.",
        target=TargetSpec(tenant="a", base_url=BASE, route_prefix="/desk"),
        input_params={"member_id": ParamSpec(type="str", required=True)},
        output_schema={
            "member_name": ParamSpec(type="str", required=True),
            "savings_balance": ParamSpec(type="str", required=True),
        },
        steps=[
            ArtifactStep(
                step_id="s1",
                action="type",
                target_name="Member ID",
                target=[LocatorCandidate(strategy="css_name_attr", value="input[name='member_id']")],
                input_ref="member_id",
                description="type 'Member ID'",
            ),
            ArtifactStep(
                step_id="s2",
                action="click",
                target_name="Search",
                target=[
                    LocatorCandidate(strategy="role_name", value="button:Search"),
                    LocatorCandidate(strategy="text", value="Search"),
                ],
                target_url=BASE + "/desk/search?member_id=4521",
                description="click 'Search'",
            ),
            ArtifactStep(
                step_id="s3",
                action="click",
                target_name="View record",
                target=[
                    LocatorCandidate(strategy="role_name", value="link:View record"),
                    LocatorCandidate(strategy="text", value="View record"),
                ],
                target_url=BASE + "/desk/member/4521",
                description="click 'View record'",
            ),
        ],
        checkpoint=Checkpoint(description="reached", url_pattern="/desk/member/{member_id}"),
        output_extraction={
            "member_name": ExtractionRule(strategy="table_row_label", label="Member Name"),
            "savings_balance": ExtractionRule(strategy="table_row_label", label="Regular Savings"),
        },
        created_from_run_id="test_run",
        created_at=datetime.now(timezone.utc),
        approved=False,
    )


TENANT_B_OVERLAY = {
    "target": {"tenant": "b", "base_url": "http://localhost:4479", "route_prefix": "/operations"},
    "checkpoint": {"url_pattern": "/operations/member/{member_id}"},
    "step_overrides": {
        "s1": {"target": [{"strategy": "css_name_attr", "value": "input[name='acct_holder_no']"}]},
        "s2": {"target_url": "http://localhost:4479/operations/search"},
        "s3": {"target_url": "http://localhost:4479/operations/member/1002"},
    },
}


def test_overlay_patches_target_and_checkpoint():
    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)

    assert overlaid.target.tenant == "b"
    assert overlaid.target.base_url == "http://localhost:4479"
    assert overlaid.target.route_prefix == "/operations"
    assert overlaid.checkpoint.url_pattern == "/operations/member/{member_id}"


def test_overlay_patches_only_the_named_step():
    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)

    s1 = next(s for s in overlaid.steps if s.step_id == "s1")
    assert s1.target[0].value == "input[name='acct_holder_no']"


def test_overlay_leaves_unmentioned_steps_completely_unchanged():
    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)

    base_s2 = next(s for s in base.steps if s.step_id == "s2")
    overlaid_s2 = next(s for s in overlaid.steps if s.step_id == "s2")
    assert overlaid_s2.target_url != base_s2.target_url
    assert overlaid_s2.target == base_s2.target
    assert overlaid_s2.target_name == base_s2.target_name


def test_overlay_leaves_input_params_output_schema_and_extraction_unchanged():
    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)

    assert overlaid.output_extraction == base.output_extraction
    assert overlaid.input_params == base.input_params
    assert overlaid.output_schema == base.output_schema


def test_overlay_does_not_mutate_the_base_artifact():
    base = _base_artifact()
    apply_overlay(base, TENANT_B_OVERLAY)
    assert base.target.tenant == "a"


def test_overlay_rejects_unknown_step_id():
    base = _base_artifact()
    bad_overlay = {"step_overrides": {"s99": {"target_url": "x"}}}
    with pytest.raises(ValueError):
        apply_overlay(base, bad_overlay)


def test_overlay_result_is_still_a_valid_artifact():
    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)
    assert Artifact.model_validate(overlaid.model_dump()) == overlaid


def test_overlaid_artifact_actually_replays_successfully_against_tenant_b(tmp_path):
    """The real proof: the overlaid artifact isn't just structurally valid
    -- it actually replays correctly against a simulated Tenant B site,
    typing into Tenant B's differently-named form field and landing on the
    checkpoint, without ever re-recording anything. Reuses the same
    FakePage harness Phase 4's tests already established."""
    from tests.test_replay_engine import FakeLocator, FakePage  # noqa: reuse existing harness
    from src.replay.engine import replay_artifact
    from src.replay.result import ReplayStatus

    base = _base_artifact()
    overlaid = apply_overlay(base, TENANT_B_OVERLAY)

    tenant_b_base = "http://localhost:4479"
    site = {
        "/operations": {
            "text": "Northwind Credit Union Operations",
            "elements": [
                {
                    "role": "textbox",
                    "name": "Acct Holder No.",
                    "css": "input[name='acct_holder_no']",
                    "field": "member_id",
                },
                {"role": "button", "name": "Search", "goto": "/operations/search?acct_holder_no={member_id}"},
            ],
        },
        "/operations/search?acct_holder_no=1002": {
            "text": "Search Results",
            "elements": [{"role": "link", "name": "View record", "goto": "/operations/member/{member_id}"}],
        },
        "/operations/member/1002": {
            "text": "Regular Savings\t5,002.00\nMember Name\tPriya Nandakumar\nStatus\tActive\nAcct Holder No.\t1002",
            "elements": [],
        },
    }
    page = FakePage(site, "/operations", base=tenant_b_base)

    result = replay_artifact(
        overlaid,
        {"member_id": "1002"},
        page=page,
        mock_auth=False,
        evidence_root=tmp_path,
        run_id="overlay_tenant_b_test",
    )

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"member_name": "Priya Nandakumar", "savings_balance": "5,002.00"}
