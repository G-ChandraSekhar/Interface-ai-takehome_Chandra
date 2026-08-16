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
