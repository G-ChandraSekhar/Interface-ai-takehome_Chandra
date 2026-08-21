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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from src.artifact.store import load_artifact_by_id
from src.capability_api.registry import artifact_to_tool_schema, discover_artifacts
from src.capability_api.chat import capability_tools, chat, confirm
from src.capability_api.runs import dom_path, list_runs, run_detail, screenshot_path
from src.replay.engine import replay_artifact

# The CLI loads this in main(); the server never needed it until the chat
# endpoint did. Without it OPENAI_API_KEY sits in .env unread and the
# chatbot reports itself unavailable on a machine that is configured fine.
load_dotenv()

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
def list_capabilities(target: Optional[str] = None, latest_only: bool = False):
    """The catalog.

    `target` and `latest_only` are filters, not deletions: superseded
    versions and artifacts recorded against the original mock app stay in
    the catalog and stay invocable, because a version that was replaced is
    still part of the audit trail. The dashboard defaults to the current
    target's latest versions so a reviewer sees the working set rather than
    the archive.
    """
    artifacts = discover_artifacts(ARTIFACTS_DIR)

    if target:
        artifacts = [a for a in artifacts if a.target.tenant == target]

    if latest_only:
        newest = {}
        for a in artifacts:
            if a.artifact_id not in newest or a.version > newest[a.artifact_id].version:
                newest[a.artifact_id] = a
        artifacts = [newest[k] for k in sorted(newest)]

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


# ---------------------------------------------------------------------------
# Run history + evidence
# ---------------------------------------------------------------------------
#
# Read straight off the evidence directory the engine already writes. Adding
# a separate store would mean the dashboard and the audit trail could
# disagree about what happened, and the evidence bundle is the thing a
# reviewer would actually be handed.


@app.get("/runs")
def get_runs(limit: int = 200, kind: Optional[str] = None, target: Optional[str] = None):
    return {"runs": list_runs(limit=limit, kind=kind, target=target)}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    detail = run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No run " + run_id)
    return detail


@app.get("/runs/{run_id}/dom/{name}")
def get_dom_snapshot(run_id: str, name: str):
    """Serve a DOM snapshot as text, never as a live page.

    text/plain deliberately: the snapshot is a bank page complete with forms
    and scripts, and rendering it inside the console would be running the
    target's markup in the reviewer's browser. It is evidence to read, not a
    page to visit.
    """
    path = dom_path(run_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="No such snapshot.")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.get("/runs/{run_id}/screenshots/{name}")
def get_screenshot(run_id: str, name: str):
    path = screenshot_path(run_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="No screenshot " + name)
    return FileResponse(str(path), media_type="image/png")


class ChatRequest(BaseModel):
    messages: list = []
    target: Optional[str] = "meridian"


@app.post("/chat")
def post_chat(request: ChatRequest):
    """A conversational front door over this same API.

    Thin by design: it writes no tool definitions, reading the catalog
    instead, and it invokes through the same replay path as everything else
    with confirmations hardcoded off. A capability that moves money is
    therefore refused here by the policy engine, which is the correct answer
    rather than a missing feature -- see src/capability_api/chat.py.
    """
    return chat(request.messages, artifacts_dir=ARTIFACTS_DIR, target=request.target)


class ConfirmRequest(BaseModel):
    confirm_token: str


@app.post("/chat/confirm")
def post_chat_confirm(request: ConfirmRequest):
    """Run a mutating action the person confirmed with a deliberate click.

    The capability and parameters are read out of the signed token, not from
    this request body, so what runs is exactly what they were shown.
    """
    return confirm(request.confirm_token, artifacts_dir=ARTIFACTS_DIR)


@app.get("/chat/tools")
def get_chat_tools(target: Optional[str] = "meridian"):
    """What the chatbot can call -- the catalog, as the model receives it."""
    return {"tools": capability_tools(ARTIFACTS_DIR, target=target)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """The operator dashboard. A single served page against this same API --
    no build step, no second toolchain, no second language in a Python
    repo."""
    page = Path(__file__).resolve().parent / "dashboard.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="dashboard.html is missing")
    return HTMLResponse(page.read_text(encoding="utf-8"))
