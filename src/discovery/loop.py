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
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.discovery.digest import build_observation
from src.discovery.evidence import EvidenceWriter
from src.discovery.llm_openai import OpenAIDiscoveryClient
from src.discovery.prompts import build_system_prompt
from src.discovery.tools import TOOL_SCHEMAS, execute_tool
from src.guardrails.engine import PolicyEngine
from src.guardrails.result import PolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DiscoveryResult:
    status: str  # "success" | "failure" | "stuck"
    outputs: dict = field(default_factory=dict)
    step_count: int = 0
    run_dir: str = ""
    message: str = ""


def _mock_login(page, base_url: str, route_prefix: str):
    page.goto(f"{base_url}{route_prefix}/login")
    page.fill("input[name='username']", "teller1")
    page.fill("input[name='password']", "training-only")
    page.click("button[type='submit']")


def run_discovery(
    *,
    goal: str,
    base_url: str,
    route_prefix: str,
    params: dict,
    required_outputs: list[str],
    mutate_confirmed: bool = False,
    irreversible_confirmed: bool = False,
    mock_auth: bool = True,
    headless: bool = False,
    model: str | None = None,
    evidence_root: Path | None = None,
    run_id: str | None = None,
    llm_client=None,
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        if mock_auth:
            _mock_login(page, base_url, route_prefix)
            evidence.log_event("mock_auth_completed", url=page.url)

        system_prompt = build_system_prompt(goal, params, required_outputs)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        while True:
            budget = policy.check_budget(step_count, time.time() - start_time)
            if budget.decision == PolicyDecision.DENY:
                status = "stuck"
                message = budget.reason
                evidence.log_event("budget_exceeded", reason=budget.reason)
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
                    status = "stuck"
                    message = response.content or "Model stopped calling tools without finishing."
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
                if tool_call.name == "type" and "ref" in logged_args:
                    el = observation.elements.get(logged_args["ref"])
                    if el and el.name.lower() in policy.sensitive_field_names:
                        logged_args["text"] = "***REDACTED***"

                evidence.log_step(
                    step_number=step_count,
                    observation_text=observation.text,
                    assistant_content=response.content,
                    tool_name=tool_call.name,
                    tool_args=logged_args,
                    tool_result_message=result.message,
                    tool_ok=result.ok,
                    page_url=page.url,
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

                if result.is_mark_output:
                    marked_outputs[result.output_name] = result.output_value
                    evidence.log_event(
                        "output_marked", name=result.output_name, value=result.output_value
                    )

                if result.is_finish:
                    just_finished = True
                    finish_message = result.message

            if just_finished:
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
        browser.close()

    final_result = {
        "status": status,
        "message": message,
        "outputs": marked_outputs,
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
