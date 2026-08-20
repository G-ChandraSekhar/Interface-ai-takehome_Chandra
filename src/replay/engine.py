"""
Deterministic replay engine.

Given a saved artifact and a set of input parameters, replay it without
invoking an LLM for any decision. Every action still passes through the
SAME PolicyEngine.check_action() used by discovery (Phase 1) -- an
artifact's `approved` flag satisfies the mutating-tier gate, but the
irreversible tier always requires a live confirmation regardless (Phase 1's
own rule, unchanged here).

`page` can be injected for testing (see tests/test_replay_engine.py's
FakePage) -- when None, a real headless/headed Playwright browser is
launched. This keeps the engine's actual decision logic (locator fallback,
business-outcome/failure classification, checkpoint verification, output
extraction) testable without a browser, while the real end-to-end path is
still exactly what production uses.

replay_artifact() is a thin wrapper that records one line of durable history
per run; _execute_replay() below it is the engine proper and is unchanged.
See the wrapper's docstring for why the emission lives there and not inside.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from src.artifact.extract import apply_extraction
from src.artifact.schema import Artifact
from src.discovery.evidence import EvidenceWriter
from src.escalation.controller import HandoffController
from src.guardrails.engine import PolicyEngine
from src.guardrails.redact import redact_value
from src.guardrails.result import PolicyDecision, RiskTier
from src.replay.checkpoint import checkpoint_met
from src.replay.detectors import (
    detect_business_outcome,
    detect_hard_failure,
    detect_recoverable_pattern,
    detectors_from_target,
)
from src.replay.locator_resolver import ResolutionFailure, resolve_locator
from src.replay.result import FailureClass, FailureDetail, ReplayResult, ReplayStatus, StepTelemetry
from src.targets import authenticate, clear_fault_injection, load_target, set_fault_injection
from src.telemetry.record import SOURCE_REPLAY, ReplayRecord, StepObservation, append_record

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_telemetry_path() -> Path:
    """Where run history accumulates.

    Overridable by environment so a test run, or a deployment with a read-only
    checkout, can redirect history without every caller having to thread a path
    through. tests/conftest.py sets this to a temp dir so the suite never
    appends to the real file.
    """
    override = os.environ.get("REPLAY_TELEMETRY_PATH")
    return Path(override) if override else (REPO_ROOT / "telemetry" / "runs.jsonl")


def replay_artifact(
    artifact: Artifact,
    params: dict,
    *,
    telemetry_path=None,
    telemetry_source: str = SOURCE_REPLAY,
    record_telemetry: bool = True,
    **kwargs,
) -> ReplayResult:
    """Runs a replay and records one line of durable history for it.

    A wrapper rather than an emission inside _execute_replay's many return
    sites: there are fifteen ways out of that function, and a telemetry call at
    each one is fifteen chances for the next person to add a sixteenth and
    forget. Here there is exactly one exit to instrument, and the engine's own
    logic stays untouched.

    Telemetry failure never fails a replay. History is a decision-support
    signal; a full disk should not stop a teller's lookup from working.
    """
    started = time.monotonic()
    result = _execute_replay(artifact, params, **kwargs)
    duration_ms = int((time.monotonic() - started) * 1000)

    if record_telemetry:
        try:
            append_record(
                _telemetry_record(artifact, result, duration_ms, telemetry_source),
                path=telemetry_path or _default_telemetry_path(),
            )
        except Exception as exc:  # noqa: BLE001 -- deliberate, see docstring
            print("[telemetry] run not recorded: " + str(exc))

    return result


def _telemetry_record(artifact, result, duration_ms, source) -> ReplayRecord:
    steps = [
        StepObservation(
            step_id=t.step_id,
            resolved=True,
            tier=t.resolved_tier,
            strategy=t.resolved_strategy,
        )
        for t in result.step_telemetry
    ]

    # step_telemetry only ever holds steps that resolved -- a step that failed
    # the ladder returns before it is appended. Recovering the failed step from
    # the failure detail matters: "s3 stopped resolving" is the single most
    # useful thing history can tell you, and without this it would be the one
    # thing history never saw.
    if result.failure and result.failure.step_id:
        if result.failure.step_id not in {s.step_id for s in steps}:
            steps.append(
                StepObservation(
                    step_id=result.failure.step_id,
                    resolved=False,
                    failure_reason=result.failure.step_class.value,
                )
            )

    return ReplayRecord(
        run_id=Path(result.run_dir).name if result.run_dir else "unknown",
        recorded_at=ReplayRecord.utcnow_iso(),
        artifact_id=artifact.artifact_id,
        artifact_version=str(artifact.version),
        tenant=getattr(artifact.target, "tenant", None),
        source=source,
        outcome=result.status.value,
        outcome_detail=(
            result.failure.step_class.value if result.failure else result.outcome_code
        ),
        duration_ms=duration_ms,
        steps=steps,
    )


def _select_option(locator, value):
    """Choose a dropdown option by underlying value, falling back to label.

    Value first, deliberately. MERIDIAN renders a share picker whose option
    VALUE is the stable share id ("100987-S0070") while its visible LABEL
    embeds the current balance -- "100987-S0070 - Share Draft (Checking)
    ($2.25)". Selecting by label would bind a capability to a balance, so it
    would replay correctly exactly once and then silently fail to match the
    moment any money moved. Label remains the fallback for targets whose
    options carry no value attribute.
    """
    try:
        return locator.select_option(value=value)
    except Exception:
        return locator.select_option(label=value)


def _resync_after_handback(page):
    """Bring the Playwright driver's view of the page back in line with the
    browser the human was just operating.

    Playwright's sync API processes browser events on the calling thread. A
    paused run is blocked in ControlLease.wait_for_state(), so for the whole
    duration of a handoff nothing is pumped: the real Chromium navigates
    when the operator clicks, the host records the transaction, and the Page
    object learns none of it. `page.url` stays frozen on the pre-handoff
    screen and no framenavigated events arrive.

    The consequence was a run that did everything correctly and then failed:
    the operator posted a transfer, the money moved, and replay reported
    checkpoint_not_met against a URL that was two screens out of date. Worse
    than a wrong answer, because every other signal said the system was
    working.

    Discovery never showed this. It resyncs by accident -- the model's next
    turn calls build_observation(), which makes enough Playwright calls to
    drain the event stream on the way past.

    Reading location.href goes to the live browser rather than trusting
    cached state, and forces the pending events through with it.
    """
    try:
        page.wait_for_load_state()
    except Exception:
        pass
    try:
        return page.evaluate("() => window.location.href")
    except Exception:
        return page.url


def _page_text(page):
    return page.locator("body").inner_text()


def _artifact_policy_violation(artifact, action, url):
    """Returns a reason string if the artifact's own declared policy forbids
    this action, or None if it permits it (or declares no policy at all).

    Checked in ADDITION to the global operator policy, never instead of it.
    """
    policy = getattr(artifact, "policy", None)
    if policy is None:
        return None

    if action not in policy.allowed_actions:
        return (
            "Action '" + action + "' is outside this artifact's declared allowed_actions "
            + str(policy.allowed_actions)
        )

    parsed = urlparse(url)
    origin = parsed.scheme + "://" + parsed.netloc
    if origin not in policy.allowed_origins:
        return (
            "Origin '" + origin + "' is outside this artifact's declared allowed_origins "
            + str(policy.allowed_origins)
        )

    return None


def _establish_session(page, artifact, chaos="none"):
    """Sign on, driven entirely by the artifact's target configuration.

    Was `_mock_login()`, duplicated verbatim here and in discovery/loop.py
    with one target's credentials and selectors baked into both. Pointing at
    MERIDIAN would have meant editing the replay engine; it is now a YAML
    file. See src/targets.py.
    """
    return authenticate(
        page,
        artifact.target.tenant,
        base_url=artifact.target.base_url,
        route_prefix=artifact.target.route_prefix,
        chaos=chaos,
    )


class _StatusTracker:
    """Remembers the status of the last main-document response.

    Recorded for evidence, not used to classify -- see DetectorPattern's
    http_status docstring for why the page text has to decide. Attaching is
    best-effort: a test double has no .on(), and losing a status must never
    fail a replay.
    """

    def __init__(self):
        self.status = None

    def attach(self, page):
        try:
            page.on("response", self._observe)
        except Exception:
            pass
        return self

    def _observe(self, response):
        try:
            request = response.request
            if request.resource_type == "document" and request.is_navigation_request():
                self.status = response.status
        except Exception:
            pass


def _error_reference(page_text):
    """The host's own error reference, e.g. 'ERR-B041BFE4'.

    What an operator would quote to a help desk, so it belongs in the
    failure detail rather than only in a screenshot.
    """
    for line in page_text.splitlines():
        if "Reference:" in line:
            return line.split("Reference:", 1)[1].strip()
    return None


def _apply_recovery(page, pattern, artifact, evidence, chaos="none"):
    """Clear a recoverable condition per its declared RecoveryAction.

    Returns True if the flow's position was restored (or never lost).
    A pattern with no declared recovery gets the legacy behaviour: click
    through a 'Continue' and stay where the click landed.
    """
    recovery = getattr(pattern, "recovery", None)
    url_before = page.url

    if recovery is None:
        try:
            page.get_by_role("button", name="Continue").click()
        except Exception:
            page.get_by_text("Continue", exact=True).click()
        return True

    if recovery.kind == "reauthenticate":
        # Only reachable because sign-on is configuration rather than a
        # hardcoded helper -- the engine re-establishes a session on a
        # target it knows nothing about.
        _establish_session(page, artifact, chaos=chaos)
        evidence.log_event("recovery_reauthenticated", target=artifact.target.tenant)
    elif recovery.kind == "click_text":
        label = recovery.value or "Continue"
        try:
            page.get_by_role("button", name=label).click()
        except Exception:
            page.get_by_text(label, exact=True).click()
    else:
        return False

    if recovery.resume and page.url != url_before:
        # The interstitial cleared but dropped us elsewhere (MERIDIAN's
        # maintenance screen returns to /menu, a timeout to /signon).
        # Continuing from there would run the rest of the flow against the
        # wrong page while reporting a successful recovery.
        page.goto(url_before)
        evidence.log_event("recovery_resumed", url=url_before)
    return True


def _execute_replay(
    artifact: Artifact,
    params: dict,
    *,
    mutate_confirmed: bool = False,
    # Retained for API compatibility but no longer grants anything: the
    # IRREVERSIBLE tier is not flag-gated. See PolicyEngine.check_action().
    irreversible_confirmed: bool = False,
    mock_auth: bool = True,
    headless: bool = True,
    chaos: str = "none",
    error_rate: float = 0.0,
    evidence_root=None,
    run_id=None,
    page=None,
    handoff: bool = False,
    console_port: int = 4590,
) -> ReplayResult:
    policy = PolicyEngine()

    run_id = run_id or (
        "replay_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    )
    evidence_root = evidence_root or (REPO_ROOT / "evidence")
    run_dir = evidence_root / run_id
    evidence = EvidenceWriter(run_dir, policy.sensitive_field_names)

    evidence.log_event(
        "replay_started",
        artifact_id=artifact.artifact_id,
        version=artifact.version,
        params=params,
        # Which console this ran against. Discovery has always recorded it;
        # replay did not, so a reader could not tell a MERIDIAN replay from
        # a mock one without opening the artifact -- and every replay looked
        # target-less in the run history.
        target=artifact.target.tenant,
        base_url=artifact.target.base_url,
    )

    origin_check = policy.check_origin(artifact.target.base_url)
    if not origin_check.allowed:
        evidence.log_event("replay_denied", reason=origin_check.reason)
        return ReplayResult(
            status=ReplayStatus.FAILURE,
            failure=FailureDetail(
                step_class=FailureClass.POLICY_DENIED,
                step_id=None,
                expected="origin in allowlist",
                observed=origin_check.reason,
            ),
            run_dir=str(run_dir),
        )

    missing_params = [
        name
        for name, spec in artifact.input_params.items()
        if spec.required and name not in params
    ]
    if missing_params:
        evidence.log_event("replay_denied", reason="missing required params: " + str(missing_params))
        return ReplayResult(
            status=ReplayStatus.FAILURE,
            failure=FailureDetail(
                step_class=FailureClass.INVALID_INPUT,
                step_id=None,
                expected="all required input params supplied",
                observed="missing: " + str(missing_params),
            ),
            run_dir=str(run_dir),
        )

    owns_browser = page is None
    browser = None
    playwright_cm = None
    # Artifact-declared detector patterns, falling back to None (which
    # makes detect_*() below use their built-in defaults) when this
    # artifact predates the detectors field or simply declares none.
    # Artifact-declared patterns win: they are what a reviewer approved
    # for THIS capability. Failing that, the target's own configured
    # taxonomy applies, so a capability recorded against a console
    # classifies by that console's copy without declaring anything.
    # Only when neither exists do detect_*()'s built-in defaults apply.
    _detectors = artifact.detectors or detectors_from_target(artifact.target.tenant)
    business_patterns = _detectors.business_outcomes if _detectors else None
    recoverable_patterns = _detectors.recoverable if _detectors else None
    hard_failure_patterns = _detectors.hard_failures if _detectors else None
    # Defined before the try so the finally block can always reference it,
    # even if browser setup raises before it would otherwise be assigned.
    handoff_controller = None
    status_tracker = _StatusTracker()
    fault_armed = False

    try:
        if owns_browser:
            playwright_cm = sync_playwright()
            p = playwright_cm.__enter__()
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            status_tracker.attach(page)
            if mock_auth:
                _establish_session(page, artifact, chaos=chaos)
                evidence.log_event("session_established", url=page.url, chaos=chaos)

                # Arm the target's own fault injection, if it has any and the
                # caller asked for one. Done AFTER sign-on because the
                # controls live behind it, and because a fault armed before
                # authentication would break the sign-on rather than the
                # capability we mean to test.
                if chaos != "none" or error_rate:
                    armed = set_fault_injection(
                        page,
                        artifact.target.tenant,
                        base_url=artifact.target.base_url,
                        kind=chaos,
                        rate=error_rate,
                    )
                    evidence.log_event(
                        "fault_injection_armed" if armed else "fault_injection_unavailable",
                        kind=chaos,
                        error_rate=error_rate,
                    )
                    fault_armed = armed

        step_telemetry = []
        recovery_attempts = {}
        if handoff:
            handoff_controller = HandoffController(evidence, page=page)

        for step in artifact.steps:
            resolution = resolve_locator(page, step.target)

            if isinstance(resolution, ResolutionFailure):
                # Log the full per-candidate diagnostic to evidence before
                # classifying -- even when this turns out to be a business
                # outcome rather than a locator problem, knowing exactly why
                # the ladder didn't resolve is useful context.
                evidence.log_event(
                    "locator_resolution_failed",
                    step_id=step.step_id,
                    attempts=resolution.attempts_as_dicts(),
                )
                page_text = _page_text(page)
                business = detect_business_outcome(page_text, business_patterns)
                if business:
                    code, message = business
                    evidence.log_event("business_outcome", code=code, step_id=step.step_id)
                    return _finish(
                        evidence,
                        ReplayResult(
                            status=ReplayStatus.BUSINESS_OUTCOME,
                            outcome_code=code,
                            outcome_message=message,
                            step_telemetry=step_telemetry,
                            run_dir=str(run_dir),
                        ),
                        page,
                        policy,
                    )
                hard = detect_hard_failure(page_text, hard_failure_patterns)
                if hard:
                    return _finish(
                        evidence,
                        ReplayResult(
                            status=ReplayStatus.FAILURE,
                            failure=FailureDetail(
                                step_class=hard,
                                step_id=step.step_id,
                                expected=step.description,
                                observed="Application error page shown.",
                            ),
                            step_telemetry=step_telemetry,
                            run_dir=str(run_dir),
                        ),
                        page,
                        policy,
                    )
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=FailureClass.LOCATOR_NOT_FOUND,
                            step_id=step.step_id,
                            expected=step.description,
                            # Per-candidate detail, not just "nothing resolved" --
                            # tells an operator exactly what changed about the page.
                            observed=resolution.summary(),
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            loc, strategy, tier = resolution.locator, resolution.strategy, resolution.tier

            value = None
            if step.input_ref:
                if step.input_ref not in params:
                    return _finish(
                        evidence,
                        ReplayResult(
                            status=ReplayStatus.FAILURE,
                            failure=FailureDetail(
                                step_class=FailureClass.INVALID_INPUT,
                                step_id=step.step_id,
                                expected="param '" + step.input_ref + "' supplied",
                                observed="not present in invocation params",
                            ),
                            step_telemetry=step_telemetry,
                            run_dir=str(run_dir),
                        ),
                        page,
                        policy,
                    )
                value = params[step.input_ref]
            elif step.literal_value is not None:
                value = step.literal_value

            # step.target_url is the literal URL frozen from discovery (e.g.
            # ".../member/4521") -- it is NOT re-rendered with this
            # invocation's params. That's fine: it's only used to classify
            # risk TIER via path-pattern matching (see
            # config/allowlist.yaml's "/desk/member/*" wildcard), and the
            # path *shape* is identical regardless of which member_id is
            # embedded in it. It is never used to navigate -- the resolved
            # locator's own click is what actually moves the browser, and
            # may land on a different concrete URL (e.g. /member/8832) than
            # the frozen string suggests.
            check_url = step.target_url if (step.action == "click" and step.target_url) else page.url

            # Defense in depth: the artifact's OWN policy is checked first,
            # in addition to (never instead of) the global operator policy
            # below. Both must permit the action. This means a capability
            # can never quietly widen its reach if the global policy is
            # later loosened for some unrelated capability's sake -- what
            # this artifact's reviewer signed off on stays binding.
            artifact_violation = _artifact_policy_violation(artifact, step.action, check_url)
            if artifact_violation:
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=FailureClass.POLICY_DENIED,
                            step_id=step.step_id,
                            expected="action within the artifact's own declared policy",
                            observed=artifact_violation,
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            policy_check = policy.check_action(
                step.action,
                check_url,
                confirmed=mutate_confirmed or irreversible_confirmed,
                artifact_approved=artifact.approved,
            )
            if policy_check.decision != PolicyDecision.ALLOW:
                # An IRREVERSIBLE step is not a failure -- it's precisely the
                # case the human-in-the-loop path exists for. If a handoff
                # controller is available, pause and let a human take control
                # of the live session to perform (or refuse) the action
                # themselves, then resume. Without a handoff controller
                # there's no human to route to, so it correctly fails closed.
                if (
                    policy_check.risk_tier == RiskTier.IRREVERSIBLE
                    and handoff_controller is not None
                ):
                    console_url = handoff_controller.start_console(port=console_port)
                    screenshot_rel = evidence.screenshot(page, "irreversible_awaiting_operator")
                    handoff_controller.request_intervention(
                        run_id=run_id,
                        run_kind="replay",
                        goal_or_capability=artifact.artifact_id + "@" + str(artifact.version),
                        step_id=step.step_id,
                        reason=(
                            "Irreversible step '" + step.description + "' cannot run "
                            "unattended. A human must take control of the live session "
                            "to perform or refuse this action."
                        ),
                        page_url=page.url,
                        screenshot_path=screenshot_rel,
                    )
                    print("\n[HANDOFF] Replay reached an irreversible step and needs a human.")
                    print("[HANDOFF] Step: " + step.step_id + " -- " + step.description)
                    print("[HANDOFF] Open " + console_url + " and click 'Take control'.")
                    print("[HANDOFF] Perform the step in the live browser, then click 'Hand back'.\n")
                    human_actions = handoff_controller.wait_for_handback()
                    # Nothing the operator did is visible to us until the
                    # driver catches up -- see _resync_after_handback().
                    resumed_url = _resync_after_handback(page)
                    print("[HANDOFF] Control returned. Verifying the resulting state.\n")

                    evidence.log_event(
                        "irreversible_step_performed_by_human",
                        step_id=step.step_id,
                        human_actions=human_actions,
                        resumed_url=resumed_url,
                    )

                    # The human acted on the live page in place of the agent.
                    # Record telemetry for the step and move on WITHOUT the
                    # agent re-performing it -- re-clicking an irreversible
                    # control after a human already did it would be exactly
                    # the double-execution this tier exists to prevent.
                    step_telemetry.append(
                        StepTelemetry(
                            step_id=step.step_id,
                            resolved_tier=tier,
                            resolved_strategy=strategy,
                            recovery_applied=False,
                        )
                    )
                    continue

                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=FailureClass.POLICY_DENIED,
                            step_id=step.step_id,
                            expected="policy allows this action",
                            observed=policy_check.reason,
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            if step.action == "click":
                loc.click()
            elif step.action == "type":
                loc.fill(str(value))
            elif step.action == "select":
                _select_option(loc, str(value))

            page_text = _page_text(page)
            recovery_applied = False

            hard = detect_hard_failure(page_text, hard_failure_patterns)
            if hard:
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=hard,
                            step_id=step.step_id,
                            expected=step.description,
                            observed=(
                                "Application error page shown after action."
                                + (
                                    " Host reference: " + _error_reference(page_text)
                                    if _error_reference(page_text)
                                    else ""
                                )
                                + (
                                    " (HTTP " + str(status_tracker.status) + ")"
                                    if status_tracker.status
                                    else ""
                                )
                            ),
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            business = detect_business_outcome(page_text, business_patterns)
            if business:
                code, message = business
                evidence.log_event("business_outcome", code=code, step_id=step.step_id)
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.BUSINESS_OUTCOME,
                        outcome_code=code,
                        outcome_message=message,
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            recoverable_pattern = detect_recoverable_pattern(page_text, recoverable_patterns)
            if recoverable_pattern:
                recoverable = recoverable_pattern.code
                attempts = recovery_attempts.get(step.step_id, 0)
                if attempts >= policy.max_recovery_attempts_per_step:
                    return _finish(
                        evidence,
                        ReplayResult(
                            status=ReplayStatus.FAILURE,
                            failure=FailureDetail(
                                step_class=FailureClass.SESSION_RECOVERY_EXHAUSTED,
                                step_id=step.step_id,
                                expected="recoverable condition '" + recoverable + "' clears within budget",
                                observed="exceeded max_recovery_attempts_per_step",
                            ),
                            step_telemetry=step_telemetry,
                            run_dir=str(run_dir),
                        ),
                        page,
                        policy,
                    )
                recovery_attempts[step.step_id] = attempts + 1
                _apply_recovery(page, recoverable_pattern, artifact, evidence, chaos=chaos)
                recovery_applied = True
                evidence.log_event(
                    "recovery_applied",
                    step_id=step.step_id,
                    condition=recoverable,
                    kind=(
                        recoverable_pattern.recovery.kind
                        if recoverable_pattern.recovery
                        else "click_continue_legacy"
                    ),
                    http_status=status_tracker.status,
                )

            # A tier above 1 means the top-ranked locator no longer worked
            # and a fallback rescued this step -- worth logging loudly, since
            # a step that starts needing fallbacks is drifting toward
            # eventual failure even while it still "passes". `cli.py health`
            # is what turns this per-run log line into a trend across runs.
            if tier > 1:
                evidence.log_event(
                    "locator_rescued_by_fallback",
                    step_id=step.step_id,
                    resolved_tier=tier,
                    resolved_strategy=strategy,
                    attempts=resolution.attempts_as_dicts(),
                )

            step_telemetry.append(
                StepTelemetry(
                    step_id=step.step_id,
                    resolved_tier=tier,
                    resolved_strategy=strategy,
                    recovery_applied=recovery_applied,
                    rescued_from=(resolution.attempts_as_dicts() if tier > 1 else None),
                )
            )

        if not checkpoint_met(artifact.checkpoint.url_pattern, params, page.url):
            return _finish(
                evidence,
                ReplayResult(
                    status=ReplayStatus.FAILURE,
                    failure=FailureDetail(
                        step_class=FailureClass.CHECKPOINT_NOT_MET,
                        step_id=None,
                        expected=artifact.checkpoint.url_pattern,
                        observed=urlparse(page.url).path,
                    ),
                    step_telemetry=step_telemetry,
                    run_dir=str(run_dir),
                ),
                page,
                policy,
            )

        page_text = _page_text(page)
        outputs = {}
        for name, rule in artifact.output_extraction.items():
            value = apply_extraction(page_text, rule, params)
            if value is None:
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=FailureClass.EXTRACTION_FAILED,
                            step_id=None,
                            expected=(
                                rule.strategy + " rule for '" + rule.label
                                + "' resolves on the final page"
                            ),
                            observed="rule did not resolve against the page text",
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )
            outputs[name] = value

        return _finish(
            evidence,
            ReplayResult(
                status=ReplayStatus.SUCCESS,
                outputs=outputs,
                step_telemetry=step_telemetry,
                run_dir=str(run_dir),
            ),
            page,
            policy,
        )
    finally:
        # Disarm before letting go of the browser. This host is shared and
        # holds its settings in memory, so a forced fault left set would
        # silently break whoever runs next -- with nothing in THEIR evidence
        # to explain it. Best-effort: a failure to clean up must not mask the
        # result the run actually produced.
        if fault_armed and page is not None:
            try:
                clear_fault_injection(
                    page, artifact.target.tenant, base_url=artifact.target.base_url
                )
                evidence.log_event("fault_injection_cleared")
            except Exception:
                pass
        if handoff_controller is not None:
            handoff_controller.stop_console()
        if owns_browser:
            if browser:
                browser.close()
            if playwright_cm:
                playwright_cm.__exit__(None, None, None)


def _finish(evidence, result, page, policy):
    if result.status != ReplayStatus.SUCCESS:
        try:
            evidence.screenshot(page, result.status.value)
        except Exception:
            pass

    persisted_outputs = {
        k: (redact_value(str(v)) if k in policy.sensitive_output_fields else v)
        for k, v in result.outputs.items()
    }
    evidence.write_result(
        {
            "status": result.status.value,
            "outcome_code": result.outcome_code,
            "outcome_message": result.outcome_message,
            "outputs": persisted_outputs,
            "failure": (
                {
                    "class": result.failure.step_class.value,
                    "step_id": result.failure.step_id,
                    "expected": result.failure.expected,
                    "observed": result.failure.observed,
                }
                if result.failure
                else None
            ),
            "step_telemetry": [
                {
                    "step_id": t.step_id,
                    "resolved_tier": t.resolved_tier,
                    "resolved_strategy": t.resolved_strategy,
                    "recovery_applied": t.recovery_applied,
                    **({"rescued_from": t.rescued_from} if t.rescued_from else {}),
                }
                for t in result.step_telemetry
            ],
        }
    )
    evidence.log_event("replay_finished", status=result.status.value)
    return result
