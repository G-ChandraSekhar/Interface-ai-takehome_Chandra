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
    ArtifactPolicy,
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


def _success_site():
    """A complete, minimal happy-path site for member 4521 -- factored out
    since several stability tests need a fresh site instance per replay
    (page_factory), not one shared mutable dict."""
    site = _base_site()
    site["/desk/search?member_id=4521"] = _search_result_page("4521")
    site["/desk/member/4521"] = {
        "text": "Member Name\tDana Whitfield\nMember ID\t4521\nRegular Savings\t2,410.55\nStatus\tActive",
        "elements": [],
    }
    return site


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

def test_artifact_declared_detector_pattern_overrides_hardcoded_default(tmp_path):
    """Proves the actual point of this refactor: a business-outcome pattern
    declared ON THE ARTIFACT is honored, using copy that does NOT match any
    of replay/detectors.py's hardcoded default markers. Stands in for what a
    real different-vendor app's "not found" copy would look like -- this
    mock app's two tenants happen to share identical templates (see
    tenants.py's own docstring), so there's no second real vendor's copy in
    this repo to test against; this is a synthetic but honest proof that
    the mechanism itself works, not evidence of a second live vendor app."""
    from src.artifact.schema import ArtifactDetectors, DetectorPattern

    artifact = make_lookup_artifact(tmp_path).model_copy(
        update={
            "detectors": ArtifactDetectors(
                business_outcomes=[
                    DetectorPattern(
                        marker="Account record could not be located",
                        code="ACCOUNT_NOT_FOUND",
                        message="No matching account exists for the supplied identifier.",
                    )
                ],
            )
        }
    )
    site = _base_site()
    site["/desk/search?member_id=9999"] = _search_result_page("9999")
    site["/desk/member/9999"] = {
        # Deliberately does NOT contain "No record found for" -- the
        # hardcoded default marker -- only the artifact-declared one.
        "text": "Account record could not be located for the given search criteria.",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "9999"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "ACCOUNT_NOT_FOUND"


def test_without_declared_patterns_a_different_vendors_copy_is_unclassified(tmp_path):
    """The other half of the proof: the SAME different-vendor copy, against
    an artifact with no declared detectors (detectors=None, e.g. one
    distilled before this field existed), falls through to the hardcoded
    defaults and is NOT recognized as a business outcome -- it's exactly
    the gap REPORT.md's Cuts section named. This is what the previous
    test's artifact-declared pattern fixes."""
    artifact = make_lookup_artifact(tmp_path)
    assert artifact.detectors is None  # sanity check on the fixture itself

    site = _base_site()
    site["/desk/search?member_id=9999"] = _search_result_page("9999")
    site["/desk/member/9999"] = {
        "text": "Account record could not be located for the given search criteria.",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "9999"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status != ReplayStatus.BUSINESS_OUTCOME


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


# ---- Per-artifact embedded policy (defense in depth) -----------------------


def test_artifact_policy_blocks_an_action_kind_it_never_declared(tmp_path):
    """The artifact's own policy is enforced IN ADDITION to the global one.
    Here the global policy would happily allow a click, but this artifact
    only ever declared 'type' -- so the click is refused. This is what stops
    a capability quietly widening its reach if the global policy is later
    loosened for some unrelated capability."""
    artifact = make_lookup_artifact(tmp_path)
    artifact.policy = ArtifactPolicy(
        allowed_origins=[BASE],
        allowed_actions=["type"],  # deliberately omits "click"
    )
    site = _base_site()
    site["/desk/search?member_id=4521"] = _search_result_page("4521")
    site["/desk/member/4521"] = {
        "text": "Member Name\tDana Whitfield\nRegular Savings\t2,410.55",
        "elements": [],
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.POLICY_DENIED
    assert "allowed_actions" in result.failure.observed


def test_artifact_policy_blocks_an_origin_it_never_declared(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    # Global allowlist permits both 4478 and 4479, but this artifact only
    # ever declared the one origin it was actually recorded against.
    artifact.policy = ArtifactPolicy(
        allowed_origins=["http://127.0.0.1:9999"],
        allowed_actions=["type", "click"],
    )
    page = FakePage(_base_site(), "/desk")

    result = replay_artifact(
        artifact, {"member_id": "4521"}, page=page, mock_auth=False, evidence_root=tmp_path
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.POLICY_DENIED
    assert "allowed_origins" in result.failure.observed


def test_artifact_policy_permits_what_it_declared(tmp_path):
    artifact = make_lookup_artifact(tmp_path)
    artifact.policy = ArtifactPolicy(
        allowed_origins=[BASE],
        allowed_actions=["type", "click"],
    )
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


def test_artifact_without_a_policy_still_replays(tmp_path):
    """Backwards compatibility: artifacts distilled before this field
    existed have policy=None and are governed by the global policy alone."""
    artifact = make_lookup_artifact(tmp_path)
    assert artifact.policy is None
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


# ---- Irreversible-tier escalation ------------------------------------------


def _mutating_artifact(tmp_path):
    """An artifact whose final step lands on the irreversible confirm route
    (see config/allowlist.yaml's irreversible_route_patterns)."""
    return Artifact(
        artifact_id="open_sub_account",
        name="Open a sub-account",
        version=1,
        goal="Open a Holiday Savings sub-account for a member.",
        target=TargetSpec(tenant="a", base_url=BASE, route_prefix="/desk"),
        input_params={"member_id": ParamSpec(type="str", required=True)},
        output_schema={"member_name": ParamSpec(type="str", required=True)},
        steps=[
            ArtifactStep(
                step_id="s1",
                action="click",
                target_name="Confirm & Open Account",
                target=[LocatorCandidate(strategy="role_name", value="button:Confirm & Open Account")],
                target_url=BASE + "/desk/member/4521/subaccount/confirm",
                description="click 'Confirm & Open Account'",
            ),
        ],
        checkpoint=Checkpoint(description="opened", url_pattern="/desk/member/{member_id}/done"),
        output_extraction={
            "member_name": ExtractionRule(strategy="table_row_label", label="Member")
        },
        created_from_run_id="test_run",
        created_at=datetime.now(timezone.utc),
        approved=True,
    )


def test_irreversible_step_fails_closed_without_a_handoff_route(tmp_path):
    """No human to route to means it must refuse, not proceed. This is the
    'fails closed' half of the guarantee."""
    artifact = _mutating_artifact(tmp_path)
    site = {
        "/desk": {
            "text": "Confirm Sub-Account Details",
            "elements": [
                {"role": "button", "name": "Confirm & Open Account", "goto": "/desk/member/4521/done"}
            ],
        },
        "/desk/member/4521/done": {"text": "Member\tDana Whitfield", "elements": []},
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact,
        {"member_id": "4521"},
        page=page,
        mock_auth=False,
        evidence_root=tmp_path,
        # handoff not enabled
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.POLICY_DENIED
    assert "never run unattended" in result.failure.observed


def test_irreversible_step_is_not_authorizable_by_the_legacy_flag(tmp_path):
    """irreversible_confirmed is retained for API compatibility but grants
    nothing -- the tier is structurally gated, not flag-gated."""
    artifact = _mutating_artifact(tmp_path)
    site = {
        "/desk": {
            "text": "Confirm Sub-Account Details",
            "elements": [
                {"role": "button", "name": "Confirm & Open Account", "goto": "/desk/member/4521/done"}
            ],
        },
        "/desk/member/4521/done": {"text": "Member\tDana Whitfield", "elements": []},
    }
    page = FakePage(site, "/desk")

    result = replay_artifact(
        artifact,
        {"member_id": "4521"},
        page=page,
        mock_auth=False,
        evidence_root=tmp_path,
        irreversible_confirmed=True,  # explicitly passed, and must not help
        mutate_confirmed=True,
    )

    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_class == FailureClass.POLICY_DENIED


def test_irreversible_step_escalates_to_a_human_and_resumes(tmp_path):
    """The other half: WITH a handoff route, an irreversible step pauses,
    a real operator (a background thread here, hitting the real console
    over real HTTP) takes control and performs it, hands back, and replay
    resumes -- crucially WITHOUT the agent re-clicking the control the
    human already actioned."""
    import threading
    import time as _time

    import requests

    artifact = _mutating_artifact(tmp_path)
    site = {
        "/desk": {
            "text": "Confirm Sub-Account Details",
            "elements": [
                {"role": "button", "name": "Confirm & Open Account", "goto": "/desk/member/4521/done"}
            ],
        },
        "/desk/member/4521/done": {"text": "Member\tDana Whitfield", "elements": []},
    }
    page = FakePage(site, "/desk")

    def act_as_operator():
        console_url = "http://127.0.0.1:4593"
        for _ in range(150):
            try:
                status = requests.get(console_url + "/status", timeout=0.5).json()
                if status["state"] == "paused" and status["intervention"]:
                    break
            except requests.exceptions.ConnectionError:
                pass
            _time.sleep(0.1)
        else:
            raise AssertionError("Console never reached a paused intervention state")

        assert requests.post(console_url + "/take-control").json()["state"] == "human_control"
        # The human performs the irreversible action themselves in the live
        # session -- simulated here by driving the same fake page directly.
        page.goto(page.base + "/desk/member/4521/done")
        _time.sleep(0.2)
        assert requests.post(console_url + "/hand-back").json()["state"] == "resuming"

    operator = threading.Thread(target=act_as_operator)
    operator.start()

    result = replay_artifact(
        artifact,
        {"member_id": "4521"},
        page=page,
        mock_auth=False,
        evidence_root=tmp_path,
        handoff=True,
        console_port=4593,
    )

    operator.join(timeout=10)

    # The human's action moved the page to the checkpoint; replay verified
    # it and extracted the declared output without re-performing the step.
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"member_name": "Dana Whitfield"}

    log_text = (tmp_path / result.run_dir.split("/")[-1] / "log.jsonl").read_text()
    assert "intervention_created" in log_text
    assert "irreversible_step_performed_by_human" in log_text


# ---- Stability scoring -------------------------------------------------


def test_run_stability_aggregates_mixed_outcomes_correctly(tmp_path):
    """3 runs against the SAME params (member_id=4521) -- matching how
    stability checking is actually used: does this operation succeed
    reliably. Run 2 simulates a transient business-outcome condition (the
    record briefly appearing unavailable) while runs 1 and 3 succeed
    normally -- proves the aggregation math itself (success_rate,
    business_outcome_rate, per-step tier averaging) against a genuinely
    mixed sequence, not just that the loop runs without crashing."""
    from src.replay.stability import run_stability

    call_count = {"n": 0}

    def page_factory():
        call_count["n"] += 1
        site = _success_site()
        if call_count["n"] == 2:
            site["/desk/member/4521"] = {
                "text": "No record found for member 4521.",
                "elements": [],
            }
        return FakePage(site, "/desk")

    artifact = make_lookup_artifact(tmp_path)
    report = run_stability(
        artifact,
        {"member_id": "4521"},
        n=3,
        page_factory=page_factory,
        mock_auth=False,
        evidence_root=tmp_path,
    )

    assert report.runs == 3
    assert report.successes == 2
    assert report.business_outcomes == 1
    assert report.failures == 0
    assert report.success_rate == pytest.approx(2 / 3)
    assert report.business_outcome_rate == pytest.approx(1 / 3)
    assert "s1" in report.step_avg_tier
    assert report.step_avg_tier["s1"] >= 1.0


def test_stability_update_artifact_persists_report_without_touching_approved(tmp_path):
    """--update-artifact writes the computed report onto the saved
    artifact's `stability` field, but must never flip `approved` -- that
    stays a human reviewer's out-of-band decision (see ArtifactStability's
    docstring in schema.py)."""
    from src.artifact.store import save_artifact
    from src.replay.stability import run_stability

    artifact = make_lookup_artifact(tmp_path)
    assert artifact.stability is None
    assert artifact.approved is False

    artifacts_dir = tmp_path / "artifacts"
    save_artifact(artifact, artifacts_dir)

    report = run_stability(
        artifact,
        {"member_id": "4521"},
        n=2,
        page_factory=lambda: FakePage(_success_site(), "/desk"),
        mock_auth=False,
        evidence_root=tmp_path,
    )

    updated = artifact.model_copy(update={"stability": report.to_artifact_stability()})
    save_artifact(updated, artifacts_dir)

    from src.artifact.store import load_artifact_by_id

    reloaded = load_artifact_by_id(artifact.artifact_id, artifact.version, artifacts_dir)
    assert reloaded.stability is not None
    assert reloaded.stability.sample_size == 2
    assert reloaded.stability.success_rate == 1.0
    assert reloaded.approved is False  # unchanged