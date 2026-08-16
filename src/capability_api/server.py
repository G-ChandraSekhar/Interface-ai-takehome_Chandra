"""
Agent-facing capability API.

GET /capabilities lists every saved artifact as an OpenAI-style tool schema,
auto-derived from the artifact's own typed contract. POST
/capabilities/{artifact_id}/invoke runs it through the exact same
replay_artifact() the CLI uses (Phase 4) -- same three-way result, same
guardrails, same evidence trail. This is not a separate execution path; it's
a second front door onto the identical replay engine, which is deliberate:
an agent invoking a capability through this API gets exactly the same
guarantees a human running `replay` from the CLI gets.

Safety boundary, by design: this API NEVER passes mutate_confirmed or
irreversible_confirmed as caller-controlled values -- both are hardcoded to
False here. A mutating-tier artifact can only run unattended through this
API if it is itself marked `approved` (a human reviewer's decision, stored
in the artifact file, not something a calling agent can set). An
irreversible-tier artifact can NEVER run through this API at all -- it will
always come back POLICY_DENIED / REQUIRE_CONFIRMATION, by construction. That
class of action is intentionally left reachable only through the
human-supervised CLI/escalation path (Phase 6), not through an interface any
agent can call unattended. See REPORT.md's Safety section.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.artifact.store import load_artifact_by_id
from src.capability_api.registry import artifact_to_tool_schema, discover_artifacts
from src.replay.engine import replay_artifact

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = Path(os.environ.get("CAPABILITY_ARTIFACTS_DIR", str(REPO_ROOT / "artifacts")))

app = FastAPI(title="Computer-Use Capability API", version="1.0")


class InvokeRequest(BaseModel):
    params: dict = {}
    headless: bool = True


class FailureOut(BaseModel):
    step_class: str
    step_id: Optional[str] = None
    expected: str
    observed: str


class InvokeResponse(BaseModel):
    status: str
    outputs: dict = {}
    outcome_code: Optional[str] = None
    outcome_message: Optional[str] = None
    failure: Optional[FailureOut] = None
    run_dir: str = ""


@app.get("/capabilities")
def list_capabilities():
    artifacts = discover_artifacts(ARTIFACTS_DIR)
    return {"capabilities": [artifact_to_tool_schema(a) for a in artifacts]}


@app.get("/capabilities/{artifact_id}")
def get_capability(artifact_id: str, version: int = 1):
    try:
        artifact = load_artifact_by_id(artifact_id, version, ARTIFACTS_DIR)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No artifact " + artifact_id + "@" + str(version))
    return artifact_to_tool_schema(artifact)


@app.post("/capabilities/{artifact_id}/invoke", response_model=InvokeResponse)
def invoke_capability(artifact_id: str, request: InvokeRequest, version: int = 1):
    try:
        artifact = load_artifact_by_id(artifact_id, version, ARTIFACTS_DIR)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No artifact " + artifact_id + "@" + str(version))

    result = replay_artifact(
        artifact,
        request.params,
        mutate_confirmed=False,
        irreversible_confirmed=False,
        mock_auth=True,
        headless=request.headless,
    )

    failure_out = None
    if result.failure:
        failure_out = FailureOut(
            step_class=result.failure.step_class.value,
            step_id=result.failure.step_id,
            expected=result.failure.expected,
            observed=result.failure.observed,
        )

    return InvokeResponse(
        status=result.status.value,
        outputs=result.outputs,
        outcome_code=result.outcome_code,
        outcome_message=result.outcome_message,
        failure=failure_out,
        run_dir=result.run_dir,
    )
