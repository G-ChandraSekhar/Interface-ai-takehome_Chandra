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
    assert out["status"] == "invalid_confirmation"
    assert "did not match" in out["note"]


def test_a_confirmation_expires():
    stale = invoke.sign("open_sub_account", {"member_id": "4521"}, 1, int(time.time()) - 1)
    out = client.post("/capabilities/confirm", json={"confirm_token": stale}).json()
    assert out["status"] == "invalid_confirmation"
    assert "expired" in out["note"]


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
