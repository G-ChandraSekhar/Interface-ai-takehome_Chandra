"""
Declarative recovery.

The original engine had exactly one way to clear a recoverable condition:
click a control whose text is literally "Continue", then carry on from
wherever that landed. That worked on the take-home's mock app because its
session-expired interstitial returns you to the page you were on.

MERIDIAN CORE breaks both halves of that assumption, and these tests pin
down the fix:

  - A maintenance interstitial (HTTP 503) does have a "Continue", but it
    goes to /menu. Clicking it and carrying on would run the rest of the
    flow against the main menu while reporting a successful recovery.
  - A session timeout (HTTP 440) has no "Continue" at all. The session is
    gone; the only recovery is signing on again.

So recovery is now declared per detector -- a RecoveryAction with a kind and
a `resume` flag -- rather than hardcoded. `resume` re-navigates to the URL
the flow was on before recovering, which is what stops a recovery from
silently continuing against the wrong page.
"""

from __future__ import annotations

from src.artifact.schema import (
    Artifact,
    ArtifactStep,
    Checkpoint,
    DetectorPattern,
    ExtractionRule,
    LocatorCandidate,
    ParamSpec,
    RecoveryAction,
    TargetSpec,
)
from src.replay.detectors import detect_recoverable_pattern, detectors_from_target
from src.replay.engine import _apply_recovery, _error_reference

BASE = "https://web-sample.interface-hiring.com"

MAINTENANCE_PAGE = (
    "SCHEDULED MAINTENANCE IN PROGRESS\n"
    "The host is temporarily unavailable while nightly batch posting completes.\n"
    "This window normally clears within a few moments.\n"
    "Continue"
)

TIMEOUT_PAGE = (
    "YOUR SESSION HAS TIMED OUT\n"
    "For security, your session ended due to inactivity.\n"
    "Return to Sign On\n"
    "NOT SIGNED ON \xa0|\xa0 08/20/2026 17:40:42"
)

APP_ERROR_PAGE = (
    "APPLICATION ERROR\n"
    "An unexpected error occurred while processing your request.\n"
    "Reference: ERR-B041BFE4\n"
    "Please contact the Help Desk if the problem persists."
)


# ---- doubles ---------------------------------------------------------------


class RecordingLocator:
    def __init__(self, page, label, found):
        self.page, self.label, self.found = page, label, found

    def click(self):
        if not self.found:
            raise RuntimeError("no such control: " + self.label)
        self.page.clicked.append(self.label)
        # Every MERIDIAN interstitial control leaves you somewhere else.
        self.page.url = BASE + "/menu"


class RecoveryPage:
    def __init__(self, url, controls=("Continue",)):
        self.url = url
        self.controls = set(controls)
        self.clicked = []
        self.gotos = []

    def get_by_role(self, role, name=None, exact=True):
        return RecordingLocator(self, name, name in self.controls)

    def get_by_text(self, text, exact=True):
        return RecordingLocator(self, text, text in self.controls)

    def goto(self, url):
        self.gotos.append(url)
        self.url = url


class FakeEvidence:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, **fields):
        self.events.append((event_type, fields))

    def kinds(self):
        return [e for e, _ in self.events]


def make_artifact(tenant="meridian"):
    return Artifact(
        artifact_id="cap",
        name="cap",
        goal="g",
        target=TargetSpec(tenant=tenant, base_url=BASE, route_prefix=""),
        input_params={"member_id": ParamSpec()},
        output_schema={"balance": ParamSpec()},
        steps=[
            ArtifactStep(
                step_id="s1",
                action="click",
                target=[LocatorCandidate(strategy="text", value="x")],
            )
        ],
        checkpoint=Checkpoint(description="d", url_pattern="/members/{member_id}"),
        output_extraction={
            "balance": ExtractionRule(strategy="table_row_label", label="Balance")
        },
        created_from_run_id="r",
        created_at="2026-08-20T00:00:00Z",
    )


# ---- the taxonomy is read from target config, not from Python --------------


def test_meridian_declares_both_recoveries_in_config():
    detectors = detectors_from_target("meridian")
    by_code = {p.code: p for p in detectors.recoverable}

    assert by_code["maintenance_window"].recovery.kind == "click_text"
    assert by_code["maintenance_window"].recovery.value == "Continue"
    assert by_code["session_timeout"].recovery.kind == "reauthenticate"
    # Both land elsewhere, so both must re-navigate.
    assert by_code["maintenance_window"].recovery.resume is True
    assert by_code["session_timeout"].recovery.resume is True


def test_detection_matches_the_real_interstitials():
    patterns = detectors_from_target("meridian").recoverable
    assert detect_recoverable_pattern(MAINTENANCE_PAGE, patterns).code == "maintenance_window"
    assert detect_recoverable_pattern(TIMEOUT_PAGE, patterns).code == "session_timeout"
    assert detect_recoverable_pattern("MEMBER RECORD\nName:\tAda", patterns) is None


def test_an_unconfigured_target_falls_back_rather_than_crashing():
    assert detectors_from_target("no-such-target") is None


# ---- resume semantics ------------------------------------------------------


def test_click_recovery_returns_the_flow_to_where_it_was():
    """The maintenance 'Continue' goes to /menu; resume must undo that."""
    page = RecoveryPage(BASE + "/members/100234/transfer")
    pattern = DetectorPattern(
        marker="SCHEDULED MAINTENANCE IN PROGRESS",
        code="maintenance_window",
        recovery=RecoveryAction(kind="click_text", value="Continue", resume=True),
    )

    _apply_recovery(page, pattern, make_artifact(), FakeEvidence())

    assert page.clicked == ["Continue"]
    assert page.gotos == [BASE + "/members/100234/transfer"]
    assert page.url == BASE + "/members/100234/transfer"


def test_without_resume_the_flow_continues_from_wherever_the_click_landed():
    page = RecoveryPage(BASE + "/members/100234/transfer")
    pattern = DetectorPattern(
        marker="m",
        code="c",
        recovery=RecoveryAction(kind="click_text", value="Continue", resume=False),
    )

    _apply_recovery(page, pattern, make_artifact(), FakeEvidence())

    assert page.clicked == ["Continue"]
    assert page.gotos == []
    assert page.url == BASE + "/menu"


def test_resume_does_not_re_navigate_when_the_click_kept_us_in_place():
    """No redundant goto when the interstitial returns you where you were."""
    page = RecoveryPage(BASE + "/menu")
    pattern = DetectorPattern(
        marker="m",
        code="c",
        recovery=RecoveryAction(kind="click_text", value="Continue", resume=True),
    )

    _apply_recovery(page, pattern, make_artifact(), FakeEvidence())

    assert page.gotos == []


# ---- reauthenticate --------------------------------------------------------


def test_timeout_recovery_signs_on_again_then_resumes(monkeypatch):
    """The only recovery for a dead session, and it needs the target seam."""
    calls = {}

    def fake_session(page, artifact, chaos="none"):
        calls["target"] = artifact.target.tenant
        page.url = BASE + "/menu"
        return page.url

    monkeypatch.setattr("src.replay.engine._establish_session", fake_session)

    page = RecoveryPage(BASE + "/members/100234/transfer", controls=())
    evidence = FakeEvidence()
    pattern = DetectorPattern(
        marker="YOUR SESSION HAS TIMED OUT",
        code="session_timeout",
        recovery=RecoveryAction(kind="reauthenticate", resume=True),
    )

    _apply_recovery(page, pattern, make_artifact(), evidence)

    assert calls["target"] == "meridian"
    assert page.clicked == []  # there is no control to click on that page
    assert page.gotos == [BASE + "/members/100234/transfer"]
    assert "recovery_reauthenticated" in evidence.kinds()
    assert "recovery_resumed" in evidence.kinds()


# ---- the legacy path must keep working -------------------------------------


def test_a_pattern_with_no_declared_recovery_keeps_the_original_behaviour():
    """Artifacts distilled before RecoveryAction existed still recover."""
    page = RecoveryPage(BASE + "/desk/member/4521")
    pattern = DetectorPattern(marker="Your session has expired.", code="session_timeout")

    assert _apply_recovery(page, pattern, make_artifact("a"), FakeEvidence()) is True
    assert page.clicked == ["Continue"]
    assert page.gotos == []  # legacy behaviour never re-navigated


def test_mock_target_recovery_is_declared_not_to_resume():
    """Its interstitial returns you where you were; re-navigating would be wrong."""
    detectors = detectors_from_target("mock")
    assert detectors.recoverable[0].recovery.resume is False


# ---- the host's own error reference ----------------------------------------


def test_application_error_reference_is_recovered_for_the_failure_detail():
    assert _error_reference(APP_ERROR_PAGE) == "ERR-B041BFE4"


def test_error_reference_is_absent_without_one():
    assert _error_reference(MAINTENANCE_PAGE) is None


# ---------------------------------------------------------------------------
# An irreversible step performed by a human must still be tiered irreversible
# ---------------------------------------------------------------------------


def test_a_human_performed_step_keeps_its_irreversible_tier():
    """The step's recorded URL decides whether replay stops for a human.

    Discovery records this step as performed-by-human after a handoff. If it
    recorded where the browser ENDED UP rather than where the control POINTS,
    replay would classify it by the review screen -- which is safe -- and post
    the transaction unattended. The guarantee would be gone, and nothing about
    the artifact would look wrong.
    """
    from src.guardrails.engine import PolicyEngine
    from src.guardrails.result import PolicyDecision, RiskTier

    policy = PolicyEngine()
    base = "https://web-sample.interface-hiring.com"

    # what the bug recorded
    assert policy._risk_tier_for_path("/members/100234/transfer/review") == RiskTier.SAFE
    # what it must record
    check = policy.check_action(
        "click",
        base + "/members/100234/transfer/post",
        confirmed=True,
        artifact_approved=True,
    )
    assert check.risk_tier == RiskTier.IRREVERSIBLE
    assert check.decision == PolicyDecision.REQUIRE_CONFIRMATION


def test_every_meridian_posting_endpoint_is_irreversible():
    """Guards the allowlist against a later edit quietly demoting one."""
    from src.guardrails.engine import PolicyEngine
    from src.guardrails.result import RiskTier

    policy = PolicyEngine()
    for path in (
        "/members/100234/transfer/post",
        "/members/100234/open-share/post",
        "/members/100234/hold/post",
    ):
        assert policy._risk_tier_for_path(path) == RiskTier.IRREVERSIBLE, path


# ---------------------------------------------------------------------------
# A marker is a claim about the target's copy, and it can be wrong
# ---------------------------------------------------------------------------


REJECTION_SCREENS = {
    # Funds transfer, the screen the original marker was written from.
    "transfer": (
        "FUNDS TRANSFER\n"
        "The transaction could not be validated:\n"
        "Insufficient available balance in the source share.\n"
        "Return to Funds Transfer"
    ),
    # Open new share -- same failure mode, different noun. The original
    # marker said "transaction" and matched nothing here, so a correct
    # refusal by the host fell through to the locator ladder and was
    # reported as locator_not_found.
    "open_share": (
        "OPEN NEW SHARE\n"
        "The request could not be validated:\n"
        "Certificates require a minimum opening deposit of $500.00.\n"
        "Return to Open New Share"
    ),
}


def test_every_rejection_screen_classifies_as_a_business_outcome():
    from src.replay.detectors import detect_business_outcome

    patterns = detectors_from_target("meridian").business_outcomes
    for screen, text in REJECTION_SCREENS.items():
        result = detect_business_outcome(text, patterns)
        assert result is not None, screen + " screen was not classified"
        assert result[0] == "TRANSACTION_REJECTED", screen


def test_the_marker_is_not_over_fitted_to_one_screens_wording():
    """Regression: the fix is that the marker no longer names the noun.

    Pinning this because the natural way to write the pattern is to paste in
    the whole sentence from whichever screen you happened to be looking at,
    which is exactly how it broke.
    """
    patterns = detectors_from_target("meridian").business_outcomes
    validated = [p for p in patterns if "could not be validated" in p.marker]
    assert validated, "no validation marker declared"
    for p in validated:
        assert "transaction" not in p.marker.lower()
        assert "request" not in p.marker.lower()


def test_a_healthy_page_is_still_not_a_rejection():
    """A widened marker that matches everything would be worse than a narrow one."""
    from src.replay.detectors import detect_business_outcome

    patterns = detectors_from_target("meridian").business_outcomes
    healthy = "OPEN NEW SHARE\nMember 103001\nShare Type:\nInitial Deposit:\nContinue"
    assert detect_business_outcome(healthy, patterns) is None


# ---------------------------------------------------------------------------
# A warning banner is not a refusal
# ---------------------------------------------------------------------------


HOLD_FORM_AS_SUPERVISOR = (
    "PLACE ACCOUNT HOLD\n"
    "RESTRICTED FUNCTION - SUPERVISOR OVERRIDE REQUIRED\n"
    "Share:\n"
    "103001-S0070-6 - Share Draft (Checking)\n"
    "Reason Code:\n"
    "FRAUD - Suspected fraud\n"
    "Notes:\n"
    "OPR SUPER1 \xa0|\xa0 BR MAIN-001"
)

SUPERVISOR_REFUSAL = (
    "SUPERVISOR OVERRIDE REQUIRED\n"
    "Operator profile teller1 is not authorized to perform this function. "
    "A supervisor must sign on to complete this request.\n"
    "Return to previous screen"
)


def test_the_hold_form_is_not_a_refusal():
    """The form carries the phrase as a banner, for every operator.

    Matching the heading meant replay classified a healthy form as a
    permission denial and stopped before filling it in -- so
    place_account_hold could never replay at all, for anyone. Discovery could
    not have caught it: discovery has no detector layer, so the recording
    succeeded while every replay of it failed.
    """
    from src.replay.detectors import detect_business_outcome

    patterns = detectors_from_target("meridian").business_outcomes
    assert detect_business_outcome(HOLD_FORM_AS_SUPERVISOR, patterns) is None


def test_the_actual_refusal_still_classifies():
    from src.replay.detectors import detect_business_outcome

    patterns = detectors_from_target("meridian").business_outcomes
    result = detect_business_outcome(SUPERVISOR_REFUSAL, patterns)
    assert result is not None and result[0] == "SUPERVISOR_REQUIRED"


def test_no_marker_matches_a_page_that_merely_warns():
    """Guards the whole taxonomy, not just this one pattern.

    Every marker should key off something the host says when it has REFUSED,
    not something it says while inviting you to proceed. Both bugs found on
    this target were markers pasted from the first screen that happened to
    contain the words.
    """
    from src.replay.detectors import detect_business_outcome, detect_hard_failure

    detectors = detectors_from_target("meridian")
    healthy_pages = [
        HOLD_FORM_AS_SUPERVISOR,
        "FUNDS TRANSFER\nMember 100234 - Lovelace, Ada\nFrom Share:\nAmount:\nMemo:\nContinue",
        "CONFIRM FUNDS TRANSFER\nIRREVERSIBLE ACTION\nAmount:\t$1.00\nPost Transfer",
        "MEMBER RECORD\nName:\tLovelace, Ada\nSHARES / BALANCES",
    ]
    for page in healthy_pages:
        assert detect_business_outcome(page, detectors.business_outcomes) is None, page[:40]
        assert detect_hard_failure(page, detectors.hard_failures) is None, page[:40]
