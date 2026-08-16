"""
Exercises src/replay/engine.py end to end with a fake Playwright Page double
-- this sandbox can't download a real browser binary, so this is how the
engine's actual decision logic (locator ladder fallback, business-outcome
short-circuiting, hard-failure detection, bounded recovery, checkpoint
verification, output extraction, and -- critically -- parameterization with
a DIFFERENT member than the one used at discovery) gets real test coverage
rather than hope. The fake models exactly the mock app's pages closely
enough to exercise every branch in engine.py; it is not a general Playwright
emulator.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.artifact.schema import (
    Artifact,
    ArtifactStep,
    Checkpoint,
    ExtractionRule,
    LocatorCandidate,
    ParamSpec,
    TargetSpec,
)
from src.replay.engine import replay_artifact
from src.replay.result import FailureClass, ReplayStatus

BASE = "http://127.0.0.1:4478"


# ---- Fake Playwright double -------------------------------------------------


class FakeLocator:
    def __init__(self, page, matches):
        self.page = page
        self.matches = matches

    def count(self):
        return len(self.matches)

    def click(self):
        elem = self.matches[0]
        if "goto" in elem:
            self.page.url = self.page._resolve(elem["goto"])
        if "recover_to" in elem:
            self.page.url = self.page._resolve(elem["recover_to"])

    def fill(self, text):
        self.page._values[self.matches[0]["field"]] = text

    def select_option(self, label=None):
        self.page._values[self.matches[0]["field"]] = label

    def inner_text(self):
        # only ever called on locator("body")
        return self.page._current_page["text"]


class FakePage:
    """site: dict[path] -> {"text": str, "elements": [ {role,name,css,field?,goto?,recover_to?} ]}
    Supports {member_id} templating in "goto"/"recover_to" targets using
    whatever was most recently .fill()-ed into a field named "member_id".
    self.url always holds a FULL absolute URL (scheme+host+path+query),
    matching real Playwright's page.url -- this matters because the policy
    engine's origin check parses scheme/netloc out of it."""

    def __init__(self, site, start_path, base=BASE):
        self.base = base
        self.site = site
        self.url = base + start_path
        self._values = {}

    def _path_of(self, url):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.path + (("?" + parsed.query) if parsed.query else "")

    def _resolve(self, target):
        rendered = target.format(**self._values) if "{" in target else target
        if rendered.startswith("http"):
            return rendered
        return self.base + rendered

    @property
    def _current_page(self):
        return self.site[self._path_of(self.url)]

    def goto(self, url):
        self.url = url if url.startswith("http") else self.base + url

    def locator(self, css):
        if css == "body":
            return FakeLocator(self, [{"_body": True}])
        elems = [e for e in self._current_page["elements"] if e.get("css") == css]
        return FakeLocator(self, elems)

    def get_by_role(self, role, name=None, exact=True):
        elems = [
            e
            for e in self._current_page["elements"]
            if e.get("role") == role and (name is None or e.get("name") == name)
        ]
        return FakeLocator(self, elems)

    def get_by_text(self, text, exact=True):
        elems = [e for e in self._current_page["elements"] if e.get("name") == text]
        return FakeLocator(self, elems)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None):
        pass


# ---- Shared artifact fixture (mirrors the real distilled artifact) --------


def make_lookup_artifact(tmp_path):
    return Artifact(
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        version=1,
        goal="Look up a member and read their name and regular savings balance.",
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
                target_url=None,
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
        checkpoint=Checkpoint(
            description="Member detail page reached.",
            url_pattern="/desk/member/{member_id}",
        ),
        output_extraction={
            "member_name": ExtractionRule(strategy="table_row_label", label="Member Name"),
            "savings_balance": ExtractionRule(strategy="table_row_label", label="Regular Savings"),
        },
        created_from_run_id="test_run",
        created_at=datetime.now(timezone.utc),
        approved=False,
    )


def _base_site():
    return {
        "/desk": {
            "text": "CorePoint Teller Desk\n\nMember Search",
            "elements": [
                {"role": "textbox", "name": "Member ID", "css": "input[name='member_id']", "field": "member_id"},
                {"role": "button", "name": "Search", "goto": "/desk/search?member_id={member_id}"},
            ],
        },
    }


def _search_result_page(member_id, goto="/desk/member/{member_id}"):
    return {
        "text": "Search Results",
        "elements": [{"role": "link", "name": "View record", "goto": goto}],
    }


# ---- Tests ------------------------------------------------------------------


def test_success_replay_with_original_discovery_member(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=4521"] = _search_result_page("4521")
    site["/desk/member/4521"] = {
        "text": "Member Name\tDana Whitfield\nMember ID\t4521\nRegular Savings\t2,410.55\nStatus\tActive",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"member_name": "Dana Whitfield", "savings_balance": "2,410.55"}


def test_success_replay_with_DIFFERENT_member_proves_parameterization(tmp_path):
    """The concrete proof the brief asks for: replay with a member NOT used
    at discovery time, and get that member's own real, different data back
    -- not the frozen discovery-time value."""
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=8832"] = _search_result_page("8832")
    site["/desk/member/8832"] = {
        "text": "Member Name\tMarcus Ojo\nMember ID\t8832\nRegular Savings\t918.20\nStatus\tActive",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "8832"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"member_name": "Marcus Ojo", "savings_balance": "918.20"}


def test_business_outcome_member_not_found(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=9999"] = _search_result_page("9999")
    site["/desk/member/9999"] = {
        "text": "No record found for Member ID 9999.",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "9999"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"


def test_business_outcome_permission_denied(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=6600"] = _search_result_page("6600")
    site["/desk/member/6600"] = {
        "text": "Access to record 6600 is restricted. Permission denied.",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "6600"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "PERMISSION_DENIED"


def test_hard_failure_app_error(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=4521"] = _search_result_page("4521")
    site["/desk/member/4521"] = {
        "text": "System Error 500: the account service is unavailable.",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.APP_ERROR


def test_recoverable_session_timeout_then_success(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    site["/desk/search?member_id=4521"] = _search_result_page(
        "4521", goto="/desk/member/4521/session_expired"
    )
    site["/desk/member/4521/session_expired"] = {
        "text": "Your session has expired.",
        "elements": [{"role": "button", "name": "Continue", "recover_to": "/desk/member/4521"}],
    }
    site["/desk/member/4521"] = {
        "text": "Member Name\tDana Whitfield\nMember ID\t4521\nRegular Savings\t2,410.55\nStatus\tActive",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["member_name"] == "Dana Whitfield"
    recovered_step = next(t for t in result.step_telemetry if t.step_id == "s3")
    assert recovered_step.recovery_applied is True


def test_locator_not_found_is_a_hard_failure(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    # break the ladder for step s1 by giving it a selector that will never match
    artifact.steps[0].target = [LocatorCandidate(strategy="css_id", value="#does-not-exist")]
    site = _base_site()
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.LOCATOR_NOT_FOUND
    assert result.failure.step_id == "s1"


def test_missing_required_param_is_invalid_input_failure(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    page = FakePage(_base_site(), "/desk")

    result = replay_artifact(artifact, {}, page=page, mock_auth=False, evidence_root=tmp_path)

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.INVALID_INPUT


def test_checkpoint_not_met_is_a_failure(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    site = _base_site()
    # every step resolves and clicks fine, but the flow lands somewhere that
    # does NOT match the checkpoint's expected url_pattern
    site["/desk/search?member_id=4521"] = _search_result_page(
        "4521", goto="/desk/member-info/{member_id}"
    )
    site["/desk/member-info/4521"] = {"text": "unexpected page shape", "elements": []}
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.CHECKPOINT_NOT_MET


def test_off_allowlist_target_is_denied(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    artifact.target.base_url = "https://evil.example.com"
    page = FakePage(_base_site(), "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.POLICY_DENIED
