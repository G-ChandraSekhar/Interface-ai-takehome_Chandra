"""
The artifact must be able to honour its own contract.

`input_params` is published in the capability catalogue as the arguments a
caller may set. Replay only ever varies a value that something in the
artifact references. So an input nothing references is a promise the
artifact cannot keep, and the way it fails is the worst available: a caller
asks to freeze share A for reason X, the run succeeds, returns a real
confirmation number, and freezes share B for reason Y instead. Every signal
reports success and the evidence bundle agrees.

Both shapes below are real. `place_account_hold@1` declared `share_id` and
`reason` while the model left both dropdowns on their pre-selected defaults;
`member_inquiry@1` declares `search_by` and never touches the dropdown, so
search-by-last-name is unreachable through a capability that advertises it.
Both were caught by a human reading the steps, which is not a control.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.artifact.distill import DistillationError, distill_run

BASE = "https://web-sample.interface-hiring.com"


def _step(tool, name, url=None, text=None, option=None):
    args = {}
    if text is not None:
        args["text"] = text
    if option is not None:
        args["option_text"] = option
    return {
        "event": "step",
        "tool_ok": True,
        "tool_name": tool,
        "page_url": url or BASE + "/x",
        "target_name": name,
        "target_candidates": [{"strategy": "role_name", "value": "button:" + name}],
        "tool_args": args,
    }


def _log(steps, params, outputs, checkpoint_url, extraction_rule=None):
    run_dir = Path(tempfile.mkdtemp()) / "discovery_x"
    run_dir.mkdir()
    events = [
        {
            "ts": "2026-08-20T00:00:00Z",
            "event": "run_started",
            "goal": "g",
            "base_url": BASE,
            "route_prefix": "",
            "params": params,
            "required_outputs": outputs,
            "target": "meridian",
        }
    ]
    events += steps
    events.append(
        {
            "event": "output_marked",
            "name": outputs[0],
            "value": "V",
            "page_url": checkpoint_url,
            "extraction_rule": extraction_rule
            or {"strategy": "table_row_label", "label": "Confirmation"},
        }
    )
    events.append({"event": "run_finished", "status": "success"})
    (run_dir / "log.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    return run_dir / "log.jsonl"


HOLD_PARAMS = {
    "member_id": "102777",
    "share_id": "102777-S0001",
    "reason": "FRAUD",
    "notes": "demo hold",
}


def test_a_capability_cannot_declare_a_setting_no_step_uses():
    """The real place_account_hold@1: both dropdowns left on their defaults."""
    log = _log(
        [
            _step("type", "Notes", text="demo hold"),
            _step("click", "Continue", BASE + "/members/102777/hold/review"),
            _step("click", "Apply Hold", BASE + "/members/102777/hold/post"),
        ],
        HOLD_PARAMS,
        ["confirmation"],
        BASE + "/members/102777/hold/post",
    )

    with pytest.raises(DistillationError) as excinfo:
        distill_run(
            log,
            artifact_id="place_account_hold",
            name="Place account hold",
            params=HOLD_PARAMS,
            required_outputs=["confirmation"],
        )

    message = str(excinfo.value)
    assert "share_id" in message and "reason" in message
    # The message has to say what to do about it, at the moment it fires.
    assert "Re-record" in message


def test_the_message_names_only_the_unused_params():
    """member_id and notes ARE used; naming them too would send someone hunting."""
    log = _log(
        [
            _step("type", "Notes", text="demo hold"),
            _step("click", "Apply Hold", BASE + "/members/102777/hold/post"),
        ],
        HOLD_PARAMS,
        ["confirmation"],
        BASE + "/members/{member_id}/hold/post".replace("{member_id}", "102777"),
    )
    with pytest.raises(DistillationError) as excinfo:
        distill_run(
            log,
            artifact_id="x",
            name="x",
            params=HOLD_PARAMS,
            required_outputs=["confirmation"],
        )
    message = str(excinfo.value)
    assert "notes" not in message.split("--")[0]
    assert "member_id" not in message.split("--")[0]


def test_search_by_would_have_been_caught_the_same_way():
    """member_inquiry@1: advertises search by name, only ever searches by number."""
    params = {"member_id": "100234", "search_by": "number"}
    log = _log(
        [
            _step("type", "Value", text="100234"),
            _step("click", "Search", BASE + "/members?q=100234"),
            _step("click", "Select", BASE + "/members/100234"),
        ],
        params,
        ["member_name"],
        BASE + "/members/100234",
        {"strategy": "table_row_label", "label": "Name"},
    )
    with pytest.raises(DistillationError, match="search_by"):
        distill_run(
            log,
            artifact_id="member_inquiry",
            name="Member inquiry",
            params=params,
            required_outputs=["member_name"],
        )


def test_a_fully_parameterised_recording_distills():
    """The re-recorded place_account_hold@2 shape: every field set explicitly."""
    params = dict(HOLD_PARAMS, reason="LEGAL")
    log = _log(
        [
            _step("select", "Share", option="102777-S0001"),
            _step("select", "Reason Code", option="LEGAL"),
            _step("type", "Notes", text="demo hold"),
            _step("click", "Continue", BASE + "/members/102777/hold/review"),
            _step("click", "Apply Hold", BASE + "/members/102777/hold/post"),
        ],
        params,
        ["confirmation"],
        BASE + "/members/102777/hold/post",
    )
    artifact = distill_run(
        log,
        artifact_id="place_account_hold",
        name="Place account hold",
        params=params,
        required_outputs=["confirmation"],
    )
    assert set(artifact.input_params) == set(params)


# --------------------------------------------------------------------------
# The two ways a param is consumed WITHOUT any step naming it. Both must pass,
# or the check would refuse artifacts that are perfectly correct -- which is
# how a well-meant rule turns into one people work around.
# --------------------------------------------------------------------------


def test_a_param_used_only_as_a_grid_extraction_key_is_consumed():
    """check_member_balance: share_id picks the row, no step types it."""
    params = {"member_id": "100234", "share_id": "100234-S0070"}
    log = _log(
        [
            _step("type", "Value", text="100234"),
            _step("click", "Search", BASE + "/members?q=100234"),
            _step("click", "Select", BASE + "/members/100234"),
        ],
        params,
        ["share_balance"],
        BASE + "/members/100234",
        {
            "strategy": "table_grid_cell",
            "label": "Balance",
            "key_column": "Share ID",
            "key_value": "100234-S0070",
        },
    )
    artifact = distill_run(
        log,
        artifact_id="check_member_balance",
        name="Check balance",
        params=params,
        required_outputs=["share_balance"],
    )
    assert artifact.output_extraction["share_balance"].key_input_ref == "share_id"


def test_a_param_used_only_by_the_checkpoint_is_consumed():
    """The checkpoint renders with invocation params, so this is a real use."""
    params = {"member_id": "100234"}
    log = _log(
        [
            _step("type", "Value", text="100234"),
            _step("click", "Select", BASE + "/members/100234"),
        ],
        params,
        ["member_name"],
        BASE + "/members/100234",
        {"strategy": "table_row_label", "label": "Name"},
    )
    artifact = distill_run(
        log,
        artifact_id="x",
        name="x",
        params=params,
        required_outputs=["member_name"],
    )
    assert artifact.checkpoint.url_pattern == "/members/{member_id}"
