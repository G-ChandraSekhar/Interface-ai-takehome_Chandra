"""
A thin conversational front door over the capability API.

What this is
------------
Something standing in for the AI agent the whole system exists to serve: it
turns a request in plain language into a capability invocation, runs it through
the same replay path everything else uses, and reports what came back.

It writes no tool definitions of its own. `artifact_to_tool_schema()` has been
emitting OpenAI-shaped function schemas since the take-home, so the catalog IS
the tool list -- record a new capability and this can call it with no code
change here. That is the claim the capability API makes, exercised rather than
asserted.

Three tiers, not two
--------------------
The system classifies every action as safe, mutating, or irreversible, and the
middle tier exists precisely so that changing a member's phone number is not
treated like moving their money. The CLI honours that with `--mutate`, and this
does too:

- **safe** -- runs immediately.
- **mutating** -- the chatbot does NOT run it. It returns a signed pending
  action, and the person confirms it with a deliberate click. Then it runs.
- **irreversible** -- refused outright, and no amount of confirming changes
  that. It needs a human at the live session, which a chat box is not.

The first version of this collapsed mutating into irreversible and refused
both. That was safe and wrong: a teller doing a legitimate phone update was
told to go elsewhere for no reason, and the refusal even gave the irreversible
tier's justification, which does not apply. Being over-cautious is still being
inaccurate.

Why confirmation is a signed token and not the model reading "yes"
------------------------------------------------------------------
The obvious approach is to let the model notice the person agreed. That makes
the confirmation a *model judgement*, and a model can be talked into judging
almost anything -- "the user already approved this earlier", "they said yes to
the previous one". It also leaves nothing to check afterwards.

Instead the pending action is signed over its exact parameters. Confirming
returns the token, the signature is verified, and the parameters are taken from
the token rather than from anything the model says on the second turn. So the
model cannot confirm on the person's behalf, and cannot alter what gets
confirmed: change one digit of the phone number and the signature fails.

The token expires, and it can never be minted for an irreversible action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import List, Optional, Tuple

from src.artifact.store import load_artifact_by_id
from src.guardrails.engine import PolicyEngine
from src.guardrails.result import RiskTier
from src.capability_api.registry import artifact_to_tool_schema, discover_artifacts
from src.replay.engine import replay_artifact
from src.replay.result import ReplayStatus

REPO_ROOT = Path(__file__).resolve().parents[2]

# Per-process, so tokens do not survive a restart. A pending confirmation is
# a few seconds of conversational state, not something to persist.
_SIGNING_KEY = secrets.token_bytes(32)
_TOKEN_TTL_SECONDS = 300


SYSTEM_PROMPT = """You are the front desk for a credit-union operations system.

You do not browse the banking console yourself. You call recorded capabilities,
each of which replays a workflow deterministically against MERIDIAN CORE.

How to behave:
- ALWAYS call the capability. Never ask the person for permission first, and
  never decide for yourself that something is disallowed or needs approval --
  the system decides that and tells you, and it is the only thing that can
  issue a real confirmation. If you ask instead of calling, the person is left
  with a question they have no way to answer.
- Ask for a missing required argument rather than guessing it; a member number
  is not something to invent. Asking for an ARGUMENT is different from asking
  for permission: do the first, never the second.
- Report what actually came back. Give confirmation numbers and balances
  verbatim -- never round, reword, or estimate a financial figure.
- If a capability is refused or fails, say so plainly and say why, in the
  operator's terms. Do not retry it and do not suggest a workaround.
- Some values come back redacted. Say that the value is protected rather than
  guessing at it.
- Some actions change a member's record. Those are allowed, but the person
  must confirm first. When one comes back needing confirmation, say plainly
  what will change and that they need to confirm it. Do not confirm on their
  behalf, and do not treat their earlier agreement to something else as
  agreement to this.
- Keep two different things apart, and never give one's explanation for the
  other:
  * A change to a member's RECORD (a phone number, an address) can be done
    here. It just needs the person to confirm it first. Never tell them to go
    to the operator console for one of these.
  * An action that MOVES MONEY, opens an account, or freezes an account needs
    a human present at the live session, so it cannot be done here at all, and
    no amount of confirming changes that. Point to the operator console, and
    do not apologise or imply it is a temporary limitation.

Be brief and concrete. This is a teller-facing tool, not a chat assistant."""


def _client(model: Optional[str] = None):
    """The OpenAI client, or None with a reason if it isn't usable."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None, "OPENAI_API_KEY is not set, so the chatbot cannot run."
    try:
        from openai import OpenAI

        return OpenAI(), None
    except Exception as exc:  # noqa: BLE001
        return None, "OpenAI client unavailable: " + str(exc)


def capability_tools(artifacts_dir: Path, target: Optional[str] = "meridian") -> List[dict]:
    """The catalog, as tool schemas. Latest version of each capability.

    Latest-only because offering a model both funds_transfer@1 and @2 invites
    it to pick the superseded one, and nothing in the schema says which is
    current. A superseded version stays invocable over HTTP -- it just is not
    something to hand a model unprompted.
    """
    artifacts = discover_artifacts(artifacts_dir)
    if target:
        artifacts = [a for a in artifacts if a.target.tenant == target]

    newest = {}
    for artifact in artifacts:
        current = newest.get(artifact.artifact_id)
        if current is None or artifact.version > current.version:
            newest[artifact.artifact_id] = artifact

    tools = []
    for artifact in (newest[k] for k in sorted(newest)):
        schema = artifact_to_tool_schema(artifact)
        tools.append({"type": "function", "function": schema["function"]})
    return tools


def _highest_tier(artifact) -> RiskTier:
    """The riskiest thing this capability does, per the SAME policy engine
    discovery and replay use. Not a second opinion about tiers kept here --
    that would be one more place for the allowlist to be contradicted."""
    policy = PolicyEngine()
    tier = RiskTier.SAFE
    order = {RiskTier.SAFE: 0, RiskTier.MUTATING: 1, RiskTier.IRREVERSIBLE: 2}
    for step in artifact.steps:
        url = step.target_url
        if not url:
            continue
        step_tier = policy._risk_tier_for_path(policy._path_of(url))
        if order[step_tier] > order[tier]:
            tier = step_tier
    return tier


def _sign(capability: str, params: dict, expires_at: int) -> str:
    payload = json.dumps(
        {"c": capability, "p": params, "e": expires_at}, sort_keys=True
    ).encode()
    digest = hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()
    return json.dumps({"c": capability, "p": params, "e": expires_at, "s": digest})


def verify_token(token: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """(capability, params, problem). Parameters come from the TOKEN.

    Deliberately not from whatever the caller sends alongside it -- otherwise
    a confirmation for one phone number could be replayed to set another.
    """
    try:
        data = json.loads(token)
        capability, params, expires_at, signature = data["c"], data["p"], data["e"], data["s"]
    except Exception:
        return None, None, "That confirmation could not be read."

    expected = hmac.new(
        _SIGNING_KEY,
        json.dumps({"c": capability, "p": params, "e": expires_at}, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None, None, "That confirmation did not match the action it was issued for."
    if time.time() > expires_at:
        return None, None, "That confirmation expired. Ask again if you still want it."
    return capability, params, None


def _describe(result, artifact_id: str, params: dict) -> dict:
    """The invocation's outcome, in the shape the model reads back.

    Deliberately verbose about refusals. A bare "failure" gives the model
    nothing to tell the person, and a model with nothing to say tends to
    invent something.
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

    payload["evidence"] = Path(result.run_dir).name if result.run_dir else None
    return payload


def _invoke(artifact_id: str, params: dict, artifacts_dir: Path, *, confirmed: bool = False) -> dict:
    try:
        artifact = load_artifact_by_id(artifact_id, _latest_version(artifact_id, artifacts_dir), artifacts_dir)
    except FileNotFoundError:
        return {"capability": artifact_id, "status": "no_such_capability"}

    tier = _highest_tier(artifact)

    # A mutating action stops here the FIRST time and comes back as a pending
    # confirmation. `confirmed` is only ever True when it arrived via a
    # verified token, so the model cannot set it by choosing its words.
    if tier == RiskTier.MUTATING and not confirmed:
        expires_at = int(time.time()) + _TOKEN_TTL_SECONDS
        return {
            "capability": artifact_id,
            "params": params,
            "status": "needs_confirmation",
            "risk_tier": "mutating",
            "confirm_token": _sign(artifact_id, params, expires_at),
            "note": (
                "CONFIRMABLE HERE. This changes a member record, which is allowed "
                "from this conversation once the person confirms -- a confirm "
                "button is already showing next to your reply. Say what will "
                "change and ask them to confirm it. Do NOT tell them to use the "
                "operator console and do NOT say this cannot be done here; that "
                "is only true of actions that move money or freeze accounts, and "
                "this is not one of those. Do not confirm on their behalf."
            ),
        }

    result = replay_artifact(
        artifact,
        params,
        # Confirmation reaches the MUTATING tier only. `irreversible_confirmed`
        # is hardcoded False and there is no path -- no parameter, no token, no
        # phrasing -- that makes it True from here: that tier needs a human at
        # the live session, which a chat box is not.
        mutate_confirmed=confirmed,
        irreversible_confirmed=False,
        mock_auth=True,
        headless=True,
    )
    return _describe(result, artifact_id, params)


def _latest_version(artifact_id: str, artifacts_dir: Path) -> int:
    versions = []
    for path in artifacts_dir.glob(artifact_id + "@*.json"):
        try:
            versions.append(int(path.stem.split("@")[1]))
        except Exception:
            continue
    return max(versions) if versions else 1


def confirm(token: str, artifacts_dir: Optional[Path] = None) -> dict:
    """Run a mutating action the person has explicitly confirmed.

    The capability and its parameters come out of the verified token, so what
    runs is exactly what they were shown. Nothing the model said on the way
    here is consulted.
    """
    artifacts_dir = artifacts_dir or (REPO_ROOT / "artifacts")

    capability, params, problem = verify_token(token)
    if problem:
        return {"reply": problem, "invocations": []}

    outcome = _invoke(capability, params, artifacts_dir, confirmed=True)

    if outcome.get("status") == "success":
        changed = ", ".join(str(k) + " is now " + str(v) for k, v in (outcome.get("outputs") or {}).items())
        reply = "Done. " + (changed or "The record was updated.")
    elif outcome.get("status") == "business_outcome":
        reply = outcome.get("message") or "The host declined that."
    else:
        reply = "That did not go through: " + str(outcome.get("observed") or outcome.get("status"))

    return {"reply": reply, "invocations": [outcome]}


def chat(
    messages: List[dict],
    artifacts_dir: Optional[Path] = None,
    model: Optional[str] = None,
    target: Optional[str] = "meridian",
) -> dict:
    """One turn. Returns {reply, invocations, pending}.

    At most one round of tool calls, then a reply. Bounded on purpose: this is
    a demo driver over the API, and an unbounded agent loop here would be a
    second, less-supervised place where capabilities get chained together --
    which is the replay engine's job, under guardrails, with evidence.
    """
    artifacts_dir = artifacts_dir or (REPO_ROOT / "artifacts")
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")

    client, problem = _client()
    if client is None:
        return {"reply": problem, "invocations": []}

    tools = capability_tools(artifacts_dir, target=target)
    if not tools:
        return {"reply": "No capabilities are recorded for this target.", "invocations": []}

    conversation = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    invocations = []

    try:
        first = client.chat.completions.create(
            model=model, messages=conversation, tools=tools
        )
    except Exception as exc:  # noqa: BLE001
        return {"reply": "The model call failed: " + str(exc), "invocations": []}

    choice = first.choices[0].message
    conversation.append(choice.model_dump(exclude_none=True))

    if not choice.tool_calls:
        return {"reply": choice.content or "", "invocations": []}

    for call in choice.tool_calls:
        try:
            params = json.loads(call.function.arguments or "{}")
        except Exception:
            params = {}
        outcome = _invoke(call.function.name, params, artifacts_dir)
        invocations.append(outcome)
        # The model is told confirmation is needed; it is not given the token.
        # A token in the transcript is a token the model could repeat back as
        # though the person had confirmed.
        for_model = {k: v for k, v in outcome.items() if k != "confirm_token"}
        conversation.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(for_model),
            }
        )

    try:
        final = client.chat.completions.create(model=model, messages=conversation)
        reply = final.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        reply = "The capability ran, but summarising it failed: " + str(exc)

    # Surfaced separately from the reply so the interface can render a real
    # confirm control. If the token were only mentioned in prose, confirming
    # would come back as more text for the model to interpret -- which is the
    # arrangement this design exists to avoid.
    pending = next(
        (
            {
                "capability": inv["capability"],
                "params": inv["params"],
                "confirm_token": inv["confirm_token"],
            }
            for inv in invocations
            if inv.get("status") == "needs_confirmation"
        ),
        None,
    )

    return {"reply": reply, "invocations": invocations, "pending": pending}
