"""
Exercises the discovery loop's control flow with a scripted stub LLM client
standing in for OpenAI. This proves loop.py's own logic -- output marking,
finish validation, stuck detection, budget enforcement -- independent of
whether the live model call works, since that can only be verified with a
real OPENAI_API_KEY and network access (not available in this sandbox).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.discovery.llm_openai import LLMResponse, ToolCall
from src.discovery.loop import run_discovery

EVIDENCE_TEST_ROOT = Path(__file__).resolve().parents[1] / "evidence" / "_test_runs"


@pytest.fixture(autouse=True)
def cleanup_evidence():
    yield
    if EVIDENCE_TEST_ROOT.exists():
        shutil.rmtree(EVIDENCE_TEST_ROOT)


class StubLLMClient:
    """Replays a fixed sequence of tool calls, one per .decide() call,
    ignoring the actual message history (the mock app's flow is
    deterministic, so we can hardcode the expected ref at each step)."""

    def __init__(self, script: list[tuple[str, dict]]):
        self.script = script
        self.model = "stub"
        self._i = 0

    def decide(self, messages, tools) -> LLMResponse:
        if self._i >= len(self.script):
            return LLMResponse(content="I am stuck, no more scripted actions.", tool_calls=[])
        name, args = self.script[self._i]
        self._i += 1
        return LLMResponse(
            content=None,
            tool_calls=[ToolCall(id=f"call_{self._i}", name=name, arguments=args)],
            raw_assistant_message={"role": "assistant", "content": None, "tool_calls": []},
        )


def test_successful_lookup_marks_outputs_and_finishes(mock_app_a):
    script = [
        ("type", {"ref": "e1", "text": "4521"}),
        ("click", {"ref": "e2"}),
        ("click", {"ref": "e1"}),  # 'View record' on search results page
        ("mark_output", {"name": "member_name", "value": "Dana Whitfield"}),
        ("mark_output", {"name": "savings_balance", "value": "2,410.55"}),
        ("finish", {"summary": "Found both outputs."}),
    ]
    stub = StubLLMClient(script)

    result = run_discovery(
        goal="Look up member 4521 and read their name and regular savings balance.",
        base_url="http://127.0.0.1:4478",
        route_prefix="/desk",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="success_case",
    )

    assert result.status == "success"
    assert result.outputs == {"member_name": "Dana Whitfield", "savings_balance": "2,410.55"}
    assert Path(result.run_dir, "result.json").exists()
    assert Path(result.run_dir, "log.jsonl").exists()


def test_finish_before_all_outputs_marked_is_a_failure(mock_app_a):
    script = [
        ("type", {"ref": "e1", "text": "4521"}),
        ("click", {"ref": "e2"}),
        ("finish", {"summary": "Done (prematurely)."}),
    ]
    stub = StubLLMClient(script)

    result = run_discovery(
        goal="Look up member 4521 and read their name and regular savings balance.",
        base_url="http://127.0.0.1:4478",
        route_prefix="/desk",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="premature_finish_case",
    )

    assert result.status == "failure"
    assert "never marked" in result.message


def test_model_stuck_with_no_tool_calls_is_detected(mock_app_a):
    stub = StubLLMClient(script=[])  # immediately returns text-only responses

    result = run_discovery(
        goal="Look up member 4521 and read their name and regular savings balance.",
        base_url="http://127.0.0.1:4478",
        route_prefix="/desk",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="stuck_case",
    )

    assert result.status == "stuck"


def test_stuck_with_handoff_escalates_and_a_human_can_resolve_it(mock_app_a):
    """Phase 6: instead of a plain stuck-and-end, a stuck run with
    handoff=True pauses, starts the operator console, and blocks until an
    operator takes control and hands back -- exercised here via real HTTP
    calls against the real console server (not mocked), from a background
    thread standing in for the operator, while run_discovery blocks on the
    main thread exactly as it would in production.

    The stub is text-only (simulating confusion) for its first two calls,
    then plays out the real lookup script -- modeling "the model got stuck,
    a human helped, then it completed the goal" rather than getting stuck
    forever, so this test terminates instead of waiting on a second
    escalation nobody is listening for."""
    import threading
    import time as _time

    import requests

    class StubThatGetsStuckThenRecovers:
        def __init__(self, stuck_turns, script):
            self.stuck_turns = stuck_turns
            self.script = script
            self.model = "stub"
            self._calls = 0
            self._script_i = 0

        def decide(self, messages, tools):
            self._calls += 1
            if self._calls <= self.stuck_turns:
                return LLMResponse(content="I'm not sure what to do next.", tool_calls=[])
            name, args = self.script[self._script_i]
            self._script_i += 1
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="call_" + str(self._calls), name=name, arguments=args)],
                raw_assistant_message={"role": "assistant", "content": None, "tool_calls": []},
            )

    stub = StubThatGetsStuckThenRecovers(
        stuck_turns=2,
        script=[
            ("type", {"ref": "e1", "text": "4521"}),
            ("click", {"ref": "e2"}),
            ("click", {"ref": "e1"}),
            ("mark_output", {"name": "member_name", "value": "Dana Whitfield"}),
            ("mark_output", {"name": "savings_balance", "value": "2,410.55"}),
            ("finish", {"summary": "Found both outputs after the human helped."}),
        ],
    )

    def act_as_operator():
        console_url = "http://127.0.0.1:4591"
        for _ in range(100):
            try:
                status = requests.get(console_url + "/status", timeout=0.5).json()
                if status["state"] == "paused" and status["intervention"]:
                    break
            except requests.exceptions.ConnectionError:
                pass
            _time.sleep(0.1)
        else:
            raise AssertionError("Console never reached a paused intervention state")

        take = requests.post(console_url + "/take-control")
        assert take.json()["state"] == "human_control"

        _time.sleep(0.3)

        handback = requests.post(console_url + "/hand-back")
        assert handback.json()["state"] == "resuming"

    operator_thread = threading.Thread(target=act_as_operator)
    operator_thread.start()

    result = run_discovery(
        goal="Look up member 4521 and read their name and regular savings balance.",
        base_url="http://127.0.0.1:4478",
        route_prefix="/desk",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="handoff_case",
        handoff=True,
        console_port=4591,
    )

    operator_thread.join(timeout=5)

    assert result.status == "success"
    assert result.outputs == {"member_name": "Dana Whitfield", "savings_balance": "2,410.55"}

    log_text = Path(result.run_dir, "log.jsonl").read_text()
    assert "intervention_created" in log_text
    assert "operator_took_control" in log_text
    assert "operator_handed_back" in log_text


def test_denied_origin_short_circuits_before_any_steps(mock_app_a):
    stub = StubLLMClient(script=[])

    result = run_discovery(
        goal="Do something off-allowlist.",
        base_url="https://evil.example.com",
        route_prefix="/desk",
        params={},
        required_outputs=[],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="denied_origin_case",
    )

    assert result.status == "failure"
    assert result.step_count == 0


def test_sensitive_outputs_are_redacted_on_disk_but_not_in_returned_result(mock_app_a):
    import json

    script = [
        ("type", {"ref": "e1", "text": "4521"}),
        ("click", {"ref": "e2"}),
        ("click", {"ref": "e1"}),
        ("mark_output", {"name": "member_name", "value": "Dana Whitfield"}),
        ("mark_output", {"name": "savings_balance", "value": "2,410.55"}),
        ("finish", {"summary": "Found both outputs."}),
    ]
    stub = StubLLMClient(script)

    result = run_discovery(
        goal="Look up member 4521 and read their name and regular savings balance.",
        base_url="http://127.0.0.1:4478",
        route_prefix="/desk",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
        headless=True,
        llm_client=stub,
        evidence_root=EVIDENCE_TEST_ROOT,
        run_id="redaction_case",
    )

    # The caller (this test, standing in for the CLI / a calling agent)
    # legitimately gets the real values back -- that's the point of the
    # capability.
    assert result.outputs == {"member_name": "Dana Whitfield", "savings_balance": "2,410.55"}

    # But what's committed to disk must be masked -- both the per-step log
    # and the final result.json.
    result_json = json.loads(Path(result.run_dir, "result.json").read_text())
    assert result_json["outputs"]["member_name"] != "Dana Whitfield"
    assert result_json["outputs"]["savings_balance"] != "2,410.55"
    assert "Dana Whitfield" not in Path(result.run_dir, "result.json").read_text()
    assert "2,410.55" not in Path(result.run_dir, "result.json").read_text()

    log_text = Path(result.run_dir, "log.jsonl").read_text()
    output_marked_lines = [
        line for line in log_text.splitlines() if '"event": "output_marked"' in line
    ]
    assert output_marked_lines, "expected at least one output_marked log line"
    for line in output_marked_lines:
        assert "Dana Whitfield" not in line
        assert "2,410.55" not in line
