"""
Invoking a capability, from any surface.

This machinery used to live inside the chatbot, which was the wrong home:
nothing about risk tiers or signed confirmations is conversational. The
dashboard needs exactly the same thing to offer a Run button.

Keeping ONE implementation matters more here than it usually does. If the
dashboard grew its own idea of what "mutating" means, the guarantee stops
being "an irreversible action needs a human" and becomes "each surface
separately remembers to require one" -- and the first surface that forgets is
the hole.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.capability_api.server as server
from src.capability_api import invoke

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"
client = TestClient(server.app)


def test_there_is_exactly_one_place_confirmations_reach_the_engine():
    """The property the whole refactor exists to protect."""
    source = inspect.getsource(invoke.run)
    assert "irreversible_confirmed=False" in source

    whole = inspect.getsource(invoke)
    assert "irreversible_confirmed=True" not in whole

    # And no surface may pass one in.
    for entry in (invoke.prepare, invoke.confirm, invoke.run):
        assert "irreversible_confirmed" not in inspect.signature(entry).parameters


def test_the_chatbot_uses_the_same_implementation_not_a_copy():
    """A second copy is how two surfaces drift apart on what 'mutating' means."""
    from src.capability_api import chat

    source = inspect.getsource(chat)
    assert "from src.capability_api.invoke import" in source
    # The chatbot must not define its own tier logic or signing.
    assert "def highest_tier" not in source
    assert "def sign(" not in source


def test_a_required_param_is_refused_before_anything_runs():
    r = client.post("/capabilities/lookup_member_savings_balance/prepare",
                    json={"params": {}}).json()
    assert r["status"] == "missing_params"
    assert r["missing"] == ["member_id"]


def test_a_mutating_capability_comes_back_pending_and_signed():
    r = client.post("/capabilities/open_sub_account/prepare",
                    json={"params": {"member_id": "4521"}}).json()

    assert r["status"] == "needs_confirmation"
    assert r["risk_tier"] == "mutating"
    # Signed over exactly what the person will be shown.
    assert json.loads(r["confirm_token"])["p"] == {"member_id": "4521"}


def test_altering_the_confirmation_invalidates_it():
    r = client.post("/capabilities/open_sub_account/prepare",
                    json={"params": {"member_id": "4521"}}).json()
    tampered = r["confirm_token"].replace("4521", "9999")

    out = client.post("/capabilities/confirm",
                      json={"confirm_token": tampered}).json()
    # Same shape as /chat, so a caller need not know which endpoint it hit.
    assert out["invocations"] == []
    assert "did not match" in out["reply"]


def test_a_confirmation_expires():
    stale = invoke.sign("open_sub_account", {"member_id": "4521"}, 1, int(time.time()) - 1)
    out = client.post("/capabilities/confirm", json={"confirm_token": stale}).json()
    assert out["invocations"] == []
    assert "expired" in out["reply"]


def test_the_version_is_signed_too():
    """Without it, a confirmation issued for v3 could be replayed against v1 --
    a different contract, possibly with different required fields."""
    token = invoke.sign("update_member_information", {"member_id": "1"}, 3,
                        int(time.time()) + 300)
    capability, params, version, problem = invoke.verify_token(token)
    assert problem is None and version == 3

    swapped = json.loads(token)
    swapped["v"] = 1
    *_, problem = invoke.verify_token(json.dumps(swapped))
    assert problem and "did not match" in problem


def test_an_irreversible_request_is_refused_by_the_engine_not_the_surface():
    """It is NOT short-circuited in prepare().

    It goes to the engine, the engine refuses it, and the refusal gets an
    evidence bundle like any other outcome. A surface that quietly declined on
    its own would produce the right answer with no record that anyone asked.
    """
    source = inspect.getsource(invoke.prepare)
    assert "IRREVERSIBLE" not in source.split('"""')[2], (
        "prepare must not special-case the irreversible tier"
    )
    # Only the mutating tier is intercepted; everything else goes to run().
    assert source.count("RiskTier.") == 1
    assert "RiskTier.MUTATING" in source


# ---------------------------------------------------------------------------
# The engine must never crash its caller
# ---------------------------------------------------------------------------


def test_an_unexpected_fault_is_a_failure_not_an_exception():
    """The most serious bug of the build.

    A dropdown value that was not one of the options made Playwright retry 64
    times over 30 seconds and then raise -- straight up through the engine and
    out of the HTTP handler as a 500, with no evidence, no failure class, and
    the browser left open.

    The three-way contract promises every run comes back as success,
    business_outcome or failure. An exception escaping breaks that promise for
    every caller at once. An unexpected fault is still a FAILURE, not an
    absence of one.
    """
    import inspect

    from src.replay import engine

    source = inspect.getsource(engine.replay_artifact)
    assert "try:" in source
    assert "_execute_replay(artifact, params, **kwargs)" in source
    assert "except Exception" in source
    # ...and it comes back as a result, not a re-raise. Checked against code
    # lines only -- the block's comment legitimately contains the word
    # "raised", describing the bug this exists to prevent.
    block = source.split("except Exception")[1].split("duration_ms")[0]
    code = [l for l in block.splitlines() if not l.strip().startswith("#")]
    assert not any(l.strip().startswith("raise") for l in code)
    assert "result = ReplayResult(" in block


def test_a_bad_dropdown_value_is_the_callers_fault_not_an_app_error():
    """invalid_input, with the options they could have used -- the fix is
    nearly always to pick one of them."""
    import inspect

    from src.replay import engine

    assert issubclass(engine.InvalidOptionError, ValueError)

    source = inspect.getsource(engine.replay_artifact)
    assert "InvalidOptionError" in source
    assert "FailureClass.INVALID_INPUT" in source

    select = inspect.getsource(engine._select_option)
    assert "Available: " in select, "the error must name what the target offers"
    # Enumerate before deciding: an option that IS present gets a proper wait,
    # and only a genuinely absent one is called the caller's mistake.
    assert "by_value = any(" in select
    # Compare against a CALL, not the function's own name -- "select_option"
    # appears at index 5 in "def _select_option".
    assert select.index("present = []") < select.index("locator.select_option(")


def test_a_param_carries_an_example_of_a_valid_value():
    """A caller typed "S0070" into a share field whose options read
    "100234-S0070". The contract said the parameter was a string, which was
    true and useless.

    The example is a placeholder, never a default: a form that pre-fills a
    member number is a form that runs against the wrong member the moment
    someone stops reading.
    """
    from src.artifact.schema import ParamSpec

    assert "example" in ParamSpec.model_fields
    assert ParamSpec.model_fields["example"].default is None

    import inspect
    from src.artifact import distill

    assert "example=str(value) if value else None" in inspect.getsource(distill.distill_run)


def test_the_catalog_publishes_the_tier_before_anyone_invokes():
    """So a caller knows whether it will run, ask, or be refused -- rather
    than finding out after filling in five fields."""
    from src.artifact.store import load_artifact_by_id
    from src.capability_api.registry import artifact_to_tool_schema

    schema = artifact_to_tool_schema(
        load_artifact_by_id("open_sub_account", 1, ARTIFACTS)
    )
    assert schema["risk_tier"] == "mutating"


def test_a_fault_is_recorded_in_the_run_that_hit_it():
    """The outer net stopped the 500, but the run wrote no result.json -- the
    dashboard showed UNKNOWN with two events and nothing to explain it. A
    failure nobody can see is barely better than a crash.

    Caught where `evidence` is in scope and routed through _finish, so it gets
    a result file, a screenshot and the page markup like any other outcome.
    """
    import inspect

    from src.replay import engine

    source = inspect.getsource(engine._execute_replay)
    handler = source.split("except Exception as exc:")[-1]
    assert "return _finish(" in handler
    assert "FailureClass.INVALID_INPUT" in handler



def test_a_successful_confirmation_does_not_read_as_a_failure():
    """The worst failure mode in this project, twice now.

    confirm() returned the bare result while chat() returned
    {reply, invocations, pending}. The dashboard read data.invocations[0],
    found nothing, and told the person "that did not go through" about a phone
    number that had just been changed successfully.

    Same shape as the checkpoint_not_met bug: the system did the right thing
    and reported the opposite. A caller should not have to know which endpoint
    it called to read the answer.
    """
    import inspect

    from src.capability_api import chat as chat_module
    from src.capability_api import invoke

    confirm_src = inspect.getsource(invoke.confirm)
    assert '"reply"' in confirm_src and '"invocations"' in confirm_src
    assert '"Done. "' in confirm_src

    # Both surfaces hand back the same three keys.
    for source in (confirm_src, inspect.getsource(chat_module.chat)):
        for key in ('"reply"', '"invocations"', '"pending"'):
            assert key in source
