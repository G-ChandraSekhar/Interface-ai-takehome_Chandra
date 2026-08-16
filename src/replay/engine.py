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
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from src.artifact.extract import extract_by_label
from src.artifact.schema import Artifact
from src.discovery.evidence import EvidenceWriter
from src.guardrails.engine import PolicyEngine
from src.guardrails.redact import redact_value
from src.guardrails.result import PolicyDecision
from src.replay.checkpoint import checkpoint_met
from src.replay.detectors import detect_business_outcome, detect_hard_failure, detect_recoverable
from src.replay.locator_resolver import ResolutionFailure, resolve_locator
from src.replay.result import FailureClass, FailureDetail, ReplayResult, ReplayStatus, StepTelemetry

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _mock_login(page, base_url, route_prefix, chaos="none"):
    page.goto(base_url + route_prefix + "/login")
    page.fill("input[name='username']", "teller1")
    page.fill("input[name='password']", "training-only")
    # The mock app reads a 'chaos' form field at login and stores it in the
    # session for the rest of that session (mock_app/chaos.py) -- this is
    # how a replay run can deterministically exercise a recoverable
    # condition or a hard application error as committed evidence, exactly
    # as the brief's deliverable #3 asks for.
    if chaos != "none":
        page.evaluate(
            "(v) => { const f = document.querySelector('form'); "
            "const i = document.createElement('input'); "
            "i.type = 'hidden'; i.name = 'chaos'; i.value = v; f.appendChild(i); }",
            chaos,
        )
    page.click("button[type='submit']")


def replay_artifact(
    artifact: Artifact,
    params: dict,
    *,
    mutate_confirmed: bool = False,
    irreversible_confirmed: bool = False,
    mock_auth: bool = True,
    headless: bool = True,
    chaos: str = "none",
    evidence_root=None,
    run_id=None,
    page=None,
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

    try:
        if owns_browser:
            playwright_cm = sync_playwright()
            p = playwright_cm.__enter__()
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            if mock_auth:
                _mock_login(page, artifact.target.base_url, artifact.target.route_prefix, chaos=chaos)
                evidence.log_event("mock_auth_completed", url=page.url, chaos=chaos)

        step_telemetry = []
        recovery_attempts = {}

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
                business = detect_business_outcome(page_text)
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
                hard = detect_hard_failure(page_text)
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
                loc.select_option(label=str(value))

            page_text = _page_text(page)
            recovery_applied = False

            hard = detect_hard_failure(page_text)
            if hard:
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=hard,
                            step_id=step.step_id,
                            expected=step.description,
                            observed="Application error page shown after action.",
                        ),
                        step_telemetry=step_telemetry,
                        run_dir=str(run_dir),
                    ),
                    page,
                    policy,
                )

            business = detect_business_outcome(page_text)
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

            recoverable = detect_recoverable(page_text)
            if recoverable:
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
                try:
                    page.get_by_role("button", name="Continue").click()
                except Exception:
                    page.get_by_text("Continue", exact=True).click()
                recovery_applied = True
                evidence.log_event("recovery_applied", step_id=step.step_id, condition=recoverable)

            # A tier above 1 means the top-ranked locator no longer worked
            # and a fallback rescued this step -- worth logging loudly, since
            # a step that starts needing fallbacks is drifting toward
            # eventual failure even while it still "passes".
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
            value = extract_by_label(page_text, rule.label)
            if value is None:
                return _finish(
                    evidence,
                    ReplayResult(
                        status=ReplayStatus.FAILURE,
                        failure=FailureDetail(
                            step_class=FailureClass.EXTRACTION_FAILED,
                            step_id=None,
                            expected="label '" + rule.label + "' present on final page",
                            observed="label not found in page text",
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
