"""
Tests the capability API's request/response layer and the parts of
invoke() that fail fast before ever touching Playwright (missing required
param, off-allowlist origin, unknown artifact) -- these are real end-to-end
HTTP tests via FastAPI's TestClient, not mocks, because replay_artifact()
itself returns before launching a browser for these cases (see
src/replay/engine.py -- the param/origin checks run before the
`owns_browser` block). Full successful invocations still need a real
browser and are exercised via the CLI on a machine that has one (see
README.md's Phase 5 section).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import src.capability_api.server as server_module
from src.artifact.schema import (
    Artifact,
    ArtifactStep,
    Checkpoint,
    ExtractionRule,
    LocatorCandidate,
    ParamSpec,
    TargetSpec,
)
from src.artifact.store import save_artifact

BASE = "http://127.0.0.1:4478"


def _make_artifact():
    return Artifact(
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        version=1,
        goal="Look up a member and read their name and regular savings balance.",
        target=TargetSpec(tenant="a", base_url=BASE, route_prefix="/desk"),
        input_params={"member_id": ParamSpec(type="str", required=True, description="Member ID")},
        output_schema={
            "member_name": ParamSpec(type="str", required=True),
            "savings_balance": ParamSpec(type="str", required=True),
        },
        steps=[
            ArtifactStep(
                step_id="s1",
                action="type",
                target_name="Member ID",
                target=[LocatorCandidate(strategy="css_name_attr", value="input[name='member_id']")],
                input_ref="member_id",
                description="type 'Member ID'",
            ),
        ],
        checkpoint=Checkpoint(description="reached", url_pattern="/desk/member/{member_id}"),
        output_extraction={
            "member_name": ExtractionRule(strategy="table_row_label", label="Member Name"),
            "savings_balance": ExtractionRule(strategy="table_row_label", label="Regular Savings"),
        },
        created_from_run_id="test_run",
        created_at=datetime.now(timezone.utc),
        approved=False,
    )


def _client(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "artifacts"
    save_artifact(_make_artifact(), artifacts_dir)
    monkeypatch.setattr(server_module, "ARTIFACTS_DIR", artifacts_dir)
    return TestClient(server_module.app)


def test_list_capabilities_returns_schema_derived_from_artifact(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert len(caps) == 1
    cap = caps[0]
    assert cap["function"]["name"] == "lookup_member_savings_balance"
    assert cap["function"]["parameters"]["required"] == ["member_id"]
    assert cap["function"]["parameters"]["properties"]["member_id"]["type"] == "string"
    assert cap["output_schema"] == {"member_name": "string", "savings_balance": "string"}
    assert cap["version"] == 1
    assert cap["approved"] is False


def test_get_single_capability(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/capabilities/lookup_member_savings_balance")
    assert resp.status_code == 200
    assert resp.json()["function"]["name"] == "lookup_member_savings_balance"


def test_get_capability_not_found_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/capabilities/does_not_exist")
    assert resp.status_code == 404


def test_invoke_unknown_artifact_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/capabilities/does_not_exist/invoke", json={"params": {}})
    assert resp.status_code == 404


def test_invoke_missing_required_param_fails_without_a_browser(tmp_path, monkeypatch):
    """This exercises the REAL replay_artifact() through the full HTTP
    stack -- it returns INVALID_INPUT before ever launching Playwright,
    which is exactly what makes this testable here."""
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/capabilities/lookup_member_savings_balance/invoke", json={"params": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failure"
    assert body["failure"]["step_class"] == "invalid_input"


def test_invoke_off_allowlist_artifact_is_denied_without_a_browser(tmp_path, monkeypatch):
    artifact = _make_artifact()
    artifact.target.base_url = "https://evil.example.com"
    artifacts_dir = tmp_path / "artifacts"
    save_artifact(artifact, artifacts_dir)
    monkeypatch.setattr(server_module, "ARTIFACTS_DIR", artifacts_dir)
    client = TestClient(server_module.app)

    resp = client.post(
        "/capabilities/lookup_member_savings_balance/invoke",
        json={"params": {"member_id": "4521"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failure"
    assert body["failure"]["step_class"] == "policy_denied"


def test_capabilities_endpoint_empty_when_no_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "ARTIFACTS_DIR", tmp_path / "empty")
    client = TestClient(server_module.app)
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    assert resp.json()["capabilities"] == []
