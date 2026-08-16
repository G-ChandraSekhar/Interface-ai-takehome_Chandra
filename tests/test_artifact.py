from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.artifact.distill import DistillationError, distill_run
from src.artifact.schema import Artifact, LocatorCandidate
from src.artifact.store import load_artifact, save_artifact


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    run_dir = tmp_path / "discovery_20260816T082148Z_ea0a12"
    run_dir.mkdir()
    log_path = run_dir / "log.jsonl"
    with open(log_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return log_path


def _synthetic_success_log(tmp_path: Path) -> Path:
    """Mirrors the real log.jsonl shape produced by src/discovery/evidence.py
    for the 'look up member 4521' goal, including the target_name /
    target_candidates fields added for Phase 3."""
    events = [
        {
            "ts": "2026-08-16T08:21:44Z",
            "event": "run_started",
            "goal": "Look up member 4521 and read their name and regular savings balance.",
            "base_url": "http://127.0.0.1:4478",
            "route_prefix": "/desk",
            "params": {"member_id": "4521"},
            "required_outputs": ["member_name", "savings_balance"],
            "model": "gpt-4o-mini",
        },
        {"ts": "...", "event": "mock_auth_completed", "url": "http://127.0.0.1:4478/desk"},
        {
            "ts": "...",
            "event": "step",
            "step_number": 0,
            "page_url": "http://127.0.0.1:4478/desk",
            "observation": "...",
            "assistant_content": None,
            "tool_name": "type",
            "tool_args": {"ref": "e1", "text": "4521"},
            "tool_result": "Typed into e1 ('Member ID')",
            "tool_ok": True,
            "target_name": "Member ID",
            "target_candidates": [
                {"strategy": "role_name", "value": "textbox:Member ID"},
                {"strategy": "css_name_attr", "value": "input[name='member_id']"},
            ],
        },
        {
            "ts": "...",
            "event": "step",
            "step_number": 1,
            # page_url is logged AFTER the action runs (loop.py calls
            # evidence.log_step post-execute_tool) -- so this click's own
            # page_url is already its destination, the search results page.
            "page_url": "http://127.0.0.1:4478/desk/search?member_id=4521",
            "observation": "...",
            "assistant_content": None,
            "tool_name": "click",
            "tool_args": {"ref": "e2"},
            "tool_result": "Clicked e2 ('Search')",
            "tool_ok": True,
            "target_name": "Search",
            "target_candidates": [
                {"strategy": "role_name", "value": "button:Search"},
                {"strategy": "text", "value": "Search"},
            ],
        },
        {
            "ts": "...",
            "event": "step",
            "step_number": 2,
            "page_url": "http://127.0.0.1:4478/desk/member/4521",
            "observation": "...",
            "assistant_content": None,
            "tool_name": "click",
            "tool_args": {"ref": "e1"},
            "tool_result": "Clicked e1 ('View record')",
            "tool_ok": True,
            "target_name": "View record",
            "target_candidates": [
                {"strategy": "role_name", "value": "link:View record"},
                {"strategy": "text", "value": "View record"},
            ],
        },
        {
            "ts": "...",
            "event": "output_marked",
            "name": "member_name",
            "value": "Dana Whitfield",
            "page_url": "http://127.0.0.1:4478/desk/member/4521",
            "extraction_label": "Member Name",
        },
        {
            "ts": "...",
            "event": "output_marked",
            "name": "savings_balance",
            "value": "2,410.55",
            "page_url": "http://127.0.0.1:4478/desk/member/4521",
            "extraction_label": "Regular Savings",
        },
        {"ts": "...", "event": "run_finished", "status": "success"},
    ]
    return _write_log(tmp_path, events)


def test_distill_produces_valid_artifact(tmp_path):
    log_path = _synthetic_success_log(tmp_path)

    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    assert artifact.artifact_id == "lookup_member_savings_balance"
    assert artifact.version == 1
    assert artifact.target.tenant == "a"
    assert artifact.target.route_prefix == "/desk"
    assert set(artifact.input_params.keys()) == {"member_id"}
    assert set(artifact.output_schema.keys()) == {"member_name", "savings_balance"}
    assert artifact.created_from_run_id == "20260816T082148Z_ea0a12"


def test_distill_parameterizes_the_typed_member_id(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    type_step = next(s for s in artifact.steps if s.action == "type")
    assert type_step.input_ref == "member_id"
    assert type_step.literal_value is None  # parameterized, not frozen as a literal


def test_distill_preserves_locator_ladder(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    search_click = next(s for s in artifact.steps if s.target_name == "Search")
    strategies = [c.strategy for c in search_click.target]
    assert strategies == ["role_name", "text"]


def test_distill_builds_parameterized_checkpoint_url(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    assert artifact.checkpoint.url_pattern == "/desk/member/{member_id}"


def test_distill_rejects_run_that_did_not_succeed(tmp_path):
    events = [
        {
            "ts": "...",
            "event": "run_started",
            "goal": "x",
            "base_url": "http://127.0.0.1:4478",
            "route_prefix": "/desk",
        },
        {"ts": "...", "event": "run_finished", "status": "stuck"},
    ]
    log_path = _write_log(tmp_path, events)

    with pytest.raises(DistillationError):
        distill_run(
            log_path,
            artifact_id="x",
            name="x",
            params={},
            required_outputs=[],
        )


def test_distill_rejects_missing_required_output(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    with pytest.raises(DistillationError):
        distill_run(
            log_path,
            artifact_id="lookup_member_savings_balance",
            name="x",
            params={"member_id": "4521"},
            required_outputs=["member_name", "savings_balance", "account_status"],
        )


def test_distill_sets_click_target_url_from_its_own_post_action_page_url(tmp_path):
    """Regression test: target_url must come from the click step's OWN
    event.page_url (which loop.py logs post-action), not a later step's --
    otherwise a click's destination gets misattributed to the wrong step."""
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    search_step = next(s for s in artifact.steps if s.target_name == "Search")
    view_record_step = next(s for s in artifact.steps if s.target_name == "View record")

    assert search_step.target_url == "http://127.0.0.1:4478/desk/search?member_id=4521"
    assert view_record_step.target_url == "http://127.0.0.1:4478/desk/member/4521"
    assert search_step.target_url != view_record_step.target_url


def test_distill_builds_output_extraction_rules(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    assert artifact.output_extraction["member_name"].strategy == "table_row_label"
    assert artifact.output_extraction["member_name"].label == "Member Name"
    assert artifact.output_extraction["savings_balance"].label == "Regular Savings"


def test_distill_rejects_output_with_no_extraction_label(tmp_path):
    events_path = _synthetic_success_log(tmp_path)
    # Overwrite the file with one output_marked event missing extraction_label
    import json as _json

    lines = events_path.read_text().splitlines()
    events = [_json.loads(line) for line in lines]
    for e in events:
        if e.get("event") == "output_marked" and e.get("name") == "member_name":
            e.pop("extraction_label", None)
    events_path.write_text("\n".join(_json.dumps(e) for e in events) + "\n")

    with pytest.raises(DistillationError):
        distill_run(
            events_path,
            artifact_id="lookup_member_savings_balance",
            name="x",
            params={"member_id": "4521"},
            required_outputs=["member_name", "savings_balance"],
        )


def test_artifact_schema_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        LocatorCandidate(strategy="role_name", value="x", unexpected_field="nope")


def test_artifact_round_trips_through_storage(tmp_path):
    log_path = _synthetic_success_log(tmp_path)
    artifact = distill_run(
        log_path,
        artifact_id="lookup_member_savings_balance",
        name="Look up member savings balance",
        params={"member_id": "4521"},
        required_outputs=["member_name", "savings_balance"],
    )

    artifacts_dir = tmp_path / "artifacts"
    saved_path = save_artifact(artifact, artifacts_dir)
    assert saved_path.exists()
    assert saved_path.name == "lookup_member_savings_balance@1.json"

    loaded = load_artifact(saved_path)
    assert loaded == artifact
