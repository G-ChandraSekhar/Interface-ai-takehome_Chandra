"""
Discovery loop: observe -> decide -> policy-check -> act.

Login is treated as an authenticated precondition (mock_auth=True), not part
of the LLM-driven goal -- the goals we care about discovering ("look up
member X and read Y") start from an already-authenticated session, matching
how a real deployment would inject session/auth material rather than asking
an LLM to type credentials on every run. This keeps the LLM-driven portion
of the trace focused on the actual capability being recorded.

The loop ends when:
  - the model calls `finish` after marking every required output -> SUCCESS
  - the model calls `finish` early (not all outputs marked) -> FAILURE (declared done prematurely)
  - the model responds with plain text instead of a tool call, twice in a
    row -> STUCK (this is where Phase 6 escalation will eventually hook in;
    for Phase 2 we just record it as a stopping condition)
  - the step/duration budget from the policy engine is exceeded -> STUCK
  - the model reaches an irreversible action that gets blocked, and no
    --handoff route is configured to resolve it -> BLOCKED (distinct from
    FAILURE: the model may still call finish() and claim success, but that
    claim is discarded once a block like this is outstanding)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.artifact.extract import locate_value
from src.discovery.digest import build_observation
from src.discovery.evidence import EvidenceWriter
from src.discovery.llm_openai import OpenAIDiscoveryClient
from src.discovery.prompts import build_system_prompt
from src.discovery.tools import TOOL_SCHEMAS, execute_tool
from src.escalation.controller import HandoffController
from src.guardrails.engine import PolicyEngine
from src.guardrails.redact import REDACTED_PLACEHOLDER, redact_value
from src.guardrails.result import PolicyDecision
from src.targets import authenticate

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DiscoveryResult:
    status: str  # "success" | "failure" | "stuck" | "blocked"
    outputs: dict = field(default_factory=dict)
    step_count: int = 0
    run_dir: str = ""
    message: str = ""


def run_discovery(
    *,
    goal: str,
    base_url: str,
    route_prefix: str,
    params: dict,
    required_outputs: list[str],
    target_id: str = "mock",
    mutate_confirmed: bool = False,
    irreversible_confirmed: bool = False,
    mock_auth: bool = True,
    headless: bool = False,
    model: str | None = None,
    evidence_root: Path | None = None,
    run_id: str | None = None,
    llm_client=None,
    handoff: bool = False,
    console_port: int = 4590,
) -> DiscoveryResult:
    policy = PolicyEngine()
    # llm_client injection point exists so tests can exercise the full loop
    # control flow (marking outputs, finish handling, stuck detection,
    # budget limits) with a scripted stub, without needing a live
    # OPENAI_API_KEY or network access. Production callers (cli.py) leave
    # this as None and get the real OpenAI-backed client.
    llm = llm_client or OpenAIDiscoveryClient(model=model)

    run_id = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    evidence_root = evidence_root or (REPO_ROOT / "evidence")
    run_dir = evidence_root / f"discovery_{run_id}"
    evidence = EvidenceWriter(run_dir, policy.sensitive_field_names)

    evidence.log_event(
        "run_started",
        goal=goal,
        base_url=base_url,
        route_prefix=route_prefix,
        params=params,
        required_outputs=required_outputs,
        target=target_id,
        model=llm.model,
    )

    origin_check = policy.check_origin(base_url)
    if not origin_check.allowed:
        evidence.log_event("run_denied", reason=origin_check.reason)
        return DiscoveryResult(status="failure", run_dir=str(run_dir), message=origin_check.reason)

    marked_outputs: dict[str, str] = {}
    status = "stuck"
    message = ""
    step_count = 0
    consecutive_text_responses = 0
    start_time = time.time()
    # Set when an irreversible action is blocked and handoff isn't configured
    # to resolve it. Discovered by manual testing: without --handoff, the
    # model sees a "BLOCKED" tool result and is otherwise free to keep going
    # -- and it would rather call finish() with a plausible-sounding summary
    # than admit the goal wasn't actually reached (see REPORT.md's note on
    # gpt-4o-mini's behavior). The tool-level block already prevents the
    # unsafe click from ever happening; this flag prevents the *reported
    # status* from lying about that afterward. Once set it stays set for the
    # rest of the run -- there is no way to resolve an irreversible block
    # without a human, and none is coming.
    unresolved_irreversible_block = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        handoff_controller = HandoffController(evidence, page=page) if handoff else None

        if mock_auth:
            authenticate(page, target_id, base_url=base_url, route_prefix=route_prefix)
            evidence.log_event("session_established", url=page.url, target=target_id)

        def escalate(reason: str) -> bool:
            """Pauses for a human operator if handoff is enabled and returns
            True once they've resumed it; returns False (caller should treat
            this as a plain stopping condition) if handoff is disabled.
            Shared by both stopping conditions that can benefit from a human
            -- getting stuck with nothing to click, and exhausting the step
            budget -- so the escalation mechanics live in exactly one place.
            """
            if handoff_controller is None:
                return False
            console_url = handoff_controller.start_console(port=console_port)
            screenshot_rel = evidence.screenshot(page, "stuck_awaiting_operator")
            handoff_controller.request_intervention(
                run_id=run_id,
                run_kind="discovery",
                goal_or_capability=goal,
                step_id=None,
                reason=reason,
                page_url=page.url,
                screenshot_path=screenshot_rel,
            )
            print("\n[HANDOFF] Discovery needs a human: " + reason)
            print("[HANDOFF] Open " + console_url + " and click 'Take control'.")
            print("[HANDOFF] Operate the visible browser window directly, then click 'Hand back'.\n")
            # Event-driven, not polled -- see lease.py's docstring. No
            # interval to tune; wakes immediately on the actual transition.
            handoff_controller.wait_for_handback()
            print("[HANDOFF] Control returned to the agent. Resuming discovery.\n")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A human operator just took over the browser to help, then handed "
                        "control back to you. Re-examine the current page and continue "
                        "toward the goal from here."
                    ),
                }
            )
            return True

        system_prompt = build_system_prompt(goal, params, required_outputs)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        while True:
            budget = policy.check_budget(step_count, time.time() - start_time)
            if budget.decision == PolicyDecision.DENY:
                evidence.log_event("budget_exceeded", reason=budget.reason)
                if escalate(budget.reason):
                    # Give the resumed run a fresh budget window rather than
                    # immediately re-tripping the same limit on the very next
                    # iteration -- the human's time holding control doesn't
                    # count against the agent's step/duration budget.
                    step_count = 0
                    start_time = time.time()
                    continue
                status = "stuck"
                message = budget.reason
                break

            observation = build_observation(page)
            obs_message = (
                f"OBSERVATION\nURL: {observation.url}\nTitle: {observation.title}\n\n"
                f"{observation.text}"
            )
            messages.append({"role": "user", "content": obs_message})

            response = llm.decide(messages, TOOL_SCHEMAS)
            messages.append(response.raw_assistant_message)

            if not response.tool_calls:
                consecutive_text_responses += 1
                evidence.log_step(
                    step_number=step_count,
                    observation_text=observation.text,
                    assistant_content=response.content,
                    tool_name=None,
                    tool_args=None,
                    tool_result_message=None,
                    tool_ok=None,
                    page_url=page.url,
                )
                if consecutive_text_responses >= 2:
                    reason = response.content or "Model stopped calling tools without finishing."
                    if escalate(reason):
                        consecutive_text_responses = 0
                        continue
                    status = "stuck"
                    message = reason
                    evidence.screenshot(page, "stuck")
                    break
                # nudge and let it try again next loop iteration
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You must call exactly one tool. If you are stuck, explain why in "
                            "plain text instead, and I will end the run."
                        ),
                    }
                )
                continue

            consecutive_text_responses = 0
            just_finished = False
            for tool_call in response.tool_calls:
                result = execute_tool(
                    tool_call.name,
                    tool_call.arguments,
                    page=page,
                    observation=observation,
                    policy=policy,
                    mutate_confirmed=mutate_confirmed,
                    irreversible_confirmed=irreversible_confirmed,
                )

                logged_args = dict(tool_call.arguments)
                if tool_call.name == "select" and result.canonical_value:
                    # Record the option's stable value rather than the
                    # visible label the model picked -- see tools.py.
                    logged_args["option_text"] = result.canonical_value
                if tool_call.name == "type" and "ref" in logged_args:
                    el = observation.elements.get(logged_args["ref"])
                    if el and el.name.lower() in policy.sensitive_field_names:
                        logged_args["text"] = REDACTED_PLACEHOLDER

                target_name = None
                target_candidates = None
                if tool_call.name in ("click", "type", "select"):
                    el = observation.elements.get(tool_call.arguments.get("ref"))
                    if el:
                        target_name = el.name
                        target_candidates = [
                            {"strategy": c.strategy, "value": c.value} for c in el.candidates
                        ]

                evidence.log_step(
                    step_number=step_count,
                    observation_text=observation.text,
                    assistant_content=response.content,
                    tool_name=tool_call.name,
                    tool_args=logged_args,
                    tool_result_message=result.message,
                    tool_ok=result.ok,
                    page_url=page.url,
                    target_name=target_name,
                    target_candidates=target_candidates,
                )

                # Every tool_call the model made in this turn needs exactly
                # one matching tool response appended, in order, before the
                # next API call -- OpenAI rejects the request otherwise.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result.message,
                    }
                )

                # An irreversible block can only be cleared by a human taking
                # control -- the model cannot resolve it by trying something
                # different. Route it to the operator console immediately
                # rather than letting the model retry and eventually give up
                # as "stuck" (which is exactly what happened before this was
                # wired up: a live run reached the confirm step, was
                # correctly blocked, and then wasted turns before quitting).
                if result.needs_human:
                    if handoff_controller is not None:
                        escalate(result.human_reason or "Irreversible action requires a human.")

                        # The agent was refused this action and a human
                        # performed it instead -- but it IS a step of the
                        # capability, and the artifact has to contain it.
                        #
                        # Without this the distiller drops it (it filters on
                        # tool_ok, and the agent's own attempt failed), and
                        # replay would walk to the review screen, find its
                        # checkpoint unmet, and fail -- never pausing for a
                        # human, because the step it is supposed to pause at
                        # would not be in the artifact. The locator ladder
                        # recorded here is the one the agent resolved before
                        # policy stopped it, which is exactly the ladder
                        # replay needs to find the same control again.
                        #
                        # Recording it does not weaken the guarantee: the
                        # step carries its own irreversible route, so replay
                        # re-derives the same tier from policy and stops
                        # there too. What the artifact stores is where a
                        # human is required, not permission to skip one.
                        # page_url here must be the click's DESTINATION,
                        # not wherever the browser happens to sit once the
                        # human has finished. The distiller reads this field
                        # as a click's target_url, and replay classifies risk
                        # by that URL -- so recording the review screen
                        # instead of the post endpoint would tier this step
                        # SAFE and post the transaction unattended, defeating
                        # the guarantee this whole path exists to enforce.
                        # The destination is known independently of what the
                        # human did: it is the control's own href/action,
                        # captured at observation time.
                        blocked_el = observation.elements.get(
                            tool_call.arguments.get("ref")
                        )
                        destination = (
                            blocked_el.target_url
                            if blocked_el and blocked_el.target_url
                            else page.url
                        )
                        evidence.log_step(
                            step_number=step_count,
                            observation_text=observation.text,
                            assistant_content=response.content,
                            tool_name=tool_call.name,
                            tool_args=logged_args,
                            tool_result_message=(
                                "Performed by a human operator after handoff; the "
                                "agent was refused this action by policy."
                            ),
                            tool_ok=True,
                            page_url=destination,
                            target_name=target_name,
                            target_candidates=target_candidates,
                        )
                        evidence.log_event(
                            "irreversible_step_performed_by_human",
                            step_id_hint=target_name,
                            page_url=page.url,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "A human operator took control and handled the "
                                    "irreversible step themselves, then handed control back. "
                                    "Do NOT attempt that step again -- re-performing an "
                                    "irreversible action would double-execute it. Re-examine "
                                    "the current page and continue from here."
                                ),
                            }
                        )
                    else:
                        # No handoff route exists to resolve this. The click
                        # itself was already refused by execute_tool -- this
                        # only marks the run so a later finish() can't paper
                        # over the fact that the goal was never completed.
                        unresolved_irreversible_block = True
                        evidence.log_event(
                            "irreversible_block_unresolved",
                            reason=result.human_reason or result.message,
                            page_url=page.url,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "That action was blocked and cannot be completed in this "
                                    "run -- no human operator is available to take control. "
                                    "Do not claim this step succeeded. If nothing else useful "
                                    "remains to do, call finish and say plainly that the "
                                    "irreversible step could not be completed."
                                ),
                            }
                        )

                if result.is_mark_output:
                    marked_outputs[result.output_name] = result.output_value
                    # Captured here, not guessed later: at this exact
                    # moment we know both the value the model saw AND the
                    # full page text it saw it on, so we can look up which
                    # label sat next to that value -- this becomes the
                    # artifact's extraction rule for this output (Phase 4).
                    extraction_rule = locate_value(
                        observation.page_text, str(result.output_value)
                    )
                    # The in-memory `marked_outputs` above stays raw -- the
                    # caller of run_discovery (the CLI, ultimately a human or
                    # calling agent) legitimately needs the real value; that
                    # IS the capability. What gets written to disk is
                    # different: evidence and result.json are committed
                    # artifacts, so any output field named in
                    # sensitive_output_fields is masked before persistence.
                    logged_value = (
                        redact_value(str(result.output_value))
                        if result.output_name in policy.sensitive_output_fields
                        else result.output_value
                    )
                    evidence.log_event(
                        "output_marked",
                        name=result.output_name,
                        value=logged_value,
                        page_url=page.url,
                        extraction_label=(extraction_rule or {}).get("label"),
                        extraction_rule=extraction_rule,
                    )

                if result.is_finish:
                    just_finished = True
                    finish_message = result.message

            if just_finished:
                if unresolved_irreversible_block:
                    # The model's own summary is not trustworthy here -- it
                    # may (and in testing, did) claim success regardless of
                    # what actually happened. Report a distinct status so a
                    # caller can't mistake this for a completed run, no
                    # matter what finish_message says.
                    status = "blocked"
                    message = (
                        "Run reached an irreversible action that was blocked and never "
                        "completed by a human operator (no --handoff route was "
                        "available). The model's finish summary is disregarded because "
                        "the goal was not actually completed."
                    )
                    evidence.screenshot(page, "finish_after_unresolved_block")
                else:
                    missing = [o for o in required_outputs if o not in marked_outputs]
                    if missing:
                        status = "failure"
                        message = f"Model called finish but these outputs were never marked: {missing}"
                        evidence.screenshot(page, "finish_incomplete")
                    else:
                        status = "success"
                        message = finish_message
                        evidence.screenshot(page, "finish_success")
                break

            step_count += 1

        evidence.screenshot(page, "final_state")
        if handoff_controller is not None:
            handoff_controller.stop_console()
        browser.close()

    final_result = {
        "status": status,
        "message": message,
        # Persisted result.json is a committed artifact -- redact sensitive
        # output fields here too, same rule as the per-step log above. The
        # DiscoveryResult returned from this function (below) still carries
        # the raw values for the immediate caller.
        "outputs": {
            k: (redact_value(str(v)) if k in policy.sensitive_output_fields else v)
            for k, v in marked_outputs.items()
        },
        "step_count": step_count,
        "goal": goal,
        "params": params,
    }
    evidence.write_result(final_result)
    evidence.log_event("run_finished", status=status)

    return DiscoveryResult(
        status=status,
        outputs=marked_outputs,
        step_count=step_count,
        run_dir=str(run_dir),
        message=message,
    )
