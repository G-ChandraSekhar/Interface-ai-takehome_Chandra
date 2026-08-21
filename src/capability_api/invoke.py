"""
Invoking a capability, from any surface.

This was inside the chatbot, which was the wrong home for it. Nothing about
risk tiers or signed confirmations is conversational — the chatbot was simply
the first surface that needed them. The dashboard needs exactly the same
machinery to offer a Run button, and an operator console would too.

Keeping one implementation matters more here than it usually does. If the
dashboard grew its own idea of what "mutating" means, or its own confirmation
scheme, then the guarantee is no longer "an irreversible action needs a human"
— it is "each surface separately remembers to require one". The first surface
that forgets is the hole.

So every front door goes through here:

    prepare(capability, params)   →  what tier is this, and may it run?
    confirm(token)                →  run the mutating action a person approved

with `run()` underneath both, and `irreversible_confirmed=False` hardcoded in
exactly one place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

from src.artifact.store import load_artifact_by_id
from src.guardrails.engine import PolicyEngine
from src.guardrails.result import RiskTier
from src.replay.engine import replay_artifact
from src.replay.result import ReplayStatus

REPO_ROOT = Path(__file__).resolve().parents[2]

# Per-process, so a token cannot outlive a restart. A pending confirmation is
# a few seconds of interaction state, not something to persist.
_SIGNING_KEY = secrets.token_bytes(32)
_TOKEN_TTL_SECONDS = 300


# ---------------------------------------------------------------- tiers ----

def highest_tier(artifact) -> RiskTier:
    """The riskiest thing this capability does, per the SAME policy engine
    discovery and replay use.

    Not a second opinion about tiers kept here — that would be one more place
    for the allowlist to be quietly contradicted.
    """
    policy = PolicyEngine()
    order = {RiskTier.SAFE: 0, RiskTier.MUTATING: 1, RiskTier.IRREVERSIBLE: 2}
    tier = RiskTier.SAFE
    for step in artifact.steps:
        if not step.target_url:
            continue
        step_tier = policy._risk_tier_for_path(policy._path_of(step.target_url))
        if order[step_tier] > order[tier]:
            tier = step_tier
    return tier


def latest_version(artifact_id: str, artifacts_dir: Path) -> int:
    versions = []
    for path in artifacts_dir.glob(artifact_id + "@*.json"):
        try:
            versions.append(int(path.stem.split("@")[1]))
        except Exception:
            continue
    return max(versions) if versions else 1


# --------------------------------------------------------------- tokens ----

def sign(capability: str, params: dict, version: int, expires_at: int) -> str:
    payload = json.dumps(
        {"c": capability, "v": version, "p": params, "e": expires_at}, sort_keys=True
    ).encode()
    digest = hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()
    return json.dumps(
        {"c": capability, "v": version, "p": params, "e": expires_at, "s": digest}
    )


def verify_token(token: str) -> Tuple[Optional[str], Optional[dict], Optional[int], Optional[str]]:
    """(capability, params, version, problem).

    Parameters come out of the TOKEN, never from whatever is sent alongside
    it — otherwise a confirmation issued for one phone number could be
    replayed to set another.
    """
    try:
        data = json.loads(token)
        capability = data["c"]
        params = data["p"]
        version = data.get("v", 1)
        expires_at = data["e"]
        signature = data["s"]
    except Exception:
        return None, None, None, "That confirmation could not be read."

    expected = hmac.new(
        _SIGNING_KEY,
        json.dumps(
            {"c": capability, "v": version, "p": params, "e": expires_at},
            sort_keys=True,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return None, None, None, "That confirmation did not match the action it was issued for."
    if time.time() > expires_at:
        return None, None, None, "That confirmation expired. Ask again if you still want it."
    return capability, params, version, None


# ------------------------------------------------------------- executing ----

def run(artifact_id: str, params: dict, artifacts_dir: Path,
        *, version: Optional[int] = None, confirmed: bool = False) -> dict:
    """Execute a capability. The single place confirmations are passed to the
    replay engine, from every surface."""
    version = version or latest_version(artifact_id, artifacts_dir)
    try:
        artifact = load_artifact_by_id(artifact_id, version, artifacts_dir)
    except FileNotFoundError:
        return {"capability": artifact_id, "status": "no_such_capability"}

    result = replay_artifact(
        artifact,
        params,
        # `confirmed` reaches the MUTATING tier only. irreversible_confirmed
        # is hardcoded False here, in one place, and no caller on any surface
        # can set it: that tier needs a human at the live session, and neither
        # a chat box nor a form is one.
        mutate_confirmed=confirmed,
        irreversible_confirmed=False,
        mock_auth=True,
        headless=True,
    )
    return describe(result, artifact_id, params)


def describe(result, artifact_id: str, params: dict) -> dict:
    """The outcome, in the shape every surface reads back.

    Deliberately verbose about refusals: a bare "failure" gives a caller
    nothing to tell the person in front of them.
    """
    payload = {
        "capability": artifact_id,
        "params": params,
        "status": result.status.value,
    }

    if result.status == ReplayStatus.SUCCESS:
        payload["outputs"] = result.outputs
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        payload["outcome_code"] = result.outcome_code
        payload["message"] = result.outcome_message
        payload["note"] = (
            "This is a legitimate answer from the host, not an error. Report it "
            "as the result."
        )
    elif result.failure:
        payload["failure_class"] = result.failure.step_class.value
        payload["expected"] = result.failure.expected
        payload["observed"] = result.failure.observed
        if result.failure.step_class.value == "policy_denied":
            payload["note"] = (
                "The policy engine refused this action. Actions that move money "
                "or change account status require a human present at the moment "
                "they happen and cannot be completed from a conversation. Say so "
                "plainly; do not retry."
            )
        else:
            payload["note"] = (
                "This capability FAILED. Say so plainly and say which step failed. "
                "Do NOT explain the missing value as protected, redacted, private, "
                "or unavailable for policy reasons -- it is missing because the run "
                "did not complete. Do not offer a value from anywhere else."
            )

    payload["evidence"] = Path(result.run_dir).name if result.run_dir else None
    return payload


# --------------------------------------------------------------- prepare ----

def prepare(artifact_id: str, params: dict, artifacts_dir: Optional[Path] = None,
            *, version: Optional[int] = None) -> dict:
    """Decide what happens to this request, by tier.

    safe          runs now
    mutating      comes back as a signed pending action for a person to approve
    irreversible  refused, by running it and letting the policy engine say no

    That last one matters: an irreversible request is NOT short-circuited
    here. It goes to the engine, the engine refuses it, and the refusal gets
    an evidence bundle like any other outcome. A surface that quietly declined
    on its own would produce the right answer with no record that anyone
    asked.
    """
    artifacts_dir = artifacts_dir or (REPO_ROOT / "artifacts")
    version = version or latest_version(artifact_id, artifacts_dir)

    try:
        artifact = load_artifact_by_id(artifact_id, version, artifacts_dir)
    except FileNotFoundError:
        return {"capability": artifact_id, "status": "no_such_capability"}

    missing = [
        name for name, spec in artifact.input_params.items()
        if spec.required and not str(params.get(name, "")).strip()
    ]
    if missing:
        return {
            "capability": artifact_id,
            "status": "missing_params",
            "missing": missing,
            "note": "Required: " + ", ".join(missing),
        }

    supplied = {k: v for k, v in params.items() if str(v).strip() != ""}
    tier = highest_tier(artifact)

    if tier == RiskTier.MUTATING:
        expires_at = int(time.time()) + _TOKEN_TTL_SECONDS
        return {
            "capability": artifact_id,
            "version": version,
            "params": supplied,
            "status": "needs_confirmation",
            "risk_tier": "mutating",
            "confirm_token": sign(artifact_id, supplied, version, expires_at),
            "note": (
                "CONFIRMABLE HERE. This changes a member record, which is "
                "allowed once the person confirms -- the confirmation is "
                "signed over exactly these values. Say what will change and "
                "ask them to confirm it. Do NOT tell them to use the operator "
                "console and do NOT say this cannot be done here; that is only "
                "true of actions that move money or freeze accounts, and this "
                "is not one of those. Do not confirm on their behalf."
            ),
        }

    return run(artifact_id, supplied, artifacts_dir, version=version)


def confirm(token: str, artifacts_dir: Optional[Path] = None) -> dict:
    """Run a mutating action a person explicitly approved.

    The capability, its version and its parameters all come out of the
    verified token, so what runs is exactly what they were shown.
    """
    artifacts_dir = artifacts_dir or (REPO_ROOT / "artifacts")

    capability, params, version, problem = verify_token(token)
    if problem:
        return {"reply": problem, "invocations": [], "pending": None}

    outcome = run(capability, params, artifacts_dir, version=version, confirmed=True)

    # Same shape as chat(): {reply, invocations, pending}. Returning the bare
    # result made every surface guess at it -- the dashboard read
    # data.invocations[0].status, found nothing, and told the person "that did
    # not go through" about a run that had just succeeded. A caller should not
    # have to know which endpoint it called to read the answer.
    if outcome.get("status") == "success":
        changed = ", ".join(
            str(k) + " is now " + str(v)
            for k, v in (outcome.get("outputs") or {}).items()
        )
        reply = "Done. " + (changed or "The record was updated.")
    elif outcome.get("status") == "business_outcome":
        reply = outcome.get("message") or "The host declined that."
    else:
        reply = "That did not go through: " + str(
            outcome.get("observed") or outcome.get("status") or "unknown"
        )

    return {"reply": reply, "invocations": [outcome], "pending": None}
