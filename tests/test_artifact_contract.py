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


# ---------------------------------------------------------------------------
# Checkpoints for capabilities whose destination the caller cannot predict
# ---------------------------------------------------------------------------
#
# The URL pattern works whenever the destination follows from the inputs:
# search for member 100987 and the flow ends at /members/100987. Search by
# LAST NAME and it ends wherever the search resolved -- a number the caller
# does not know. The distiller has nothing to bind, so before this it froze
# the literal and the artifact replayed for exactly one surname.


from src.artifact.distill import _derive_assertions, _looks_like_an_identifier, _url_pattern_from
from src.replay.checkpoint import assertions_met, checkpoint_met
from src.artifact.schema import CheckpointAssertion


def test_a_predictable_destination_is_still_bound_to_its_param():
    """Unchanged for every capability that was already fine."""
    assert _url_pattern_from(
        "https://h/members/100234/transfer/post", {"member_id": "100234"}
    ) == "/members/{member_id}/transfer/post"


def test_an_unpredictable_destination_becomes_a_shape():
    """Search-by-name: the member number matches no supplied parameter."""
    assert _url_pattern_from(
        "https://h/members/101555", {"query": "Hopper", "search_by": "name"}
    ) == "/members/{*}"


@pytest.mark.parametrize(
    "segment,is_id",
    [("100234", True), ("members", False), ("transfer", False),
     ("open-share", False), ("review", False), ("100234-S0070", False)],
)
def test_route_words_are_not_mistaken_for_record_ids(segment, is_id):
    """A false positive here silently weakens a checkpoint that was correct."""
    assert _looks_like_an_identifier(segment) is is_id


def test_the_wildcard_matches_one_segment_not_the_rest_of_the_flow():
    """fnmatch's '*' crosses '/', which would let a run that died on the
    transfer form satisfy a checkpoint of /members/*. Measured, then rejected."""
    assert checkpoint_met("/members/{*}", {}, "https://h/members/100987")
    assert not checkpoint_met("/members/{*}", {}, "https://h/members/100987/transfer")
    assert not checkpoint_met("/members/{*}", {}, "https://h/menu")


def test_identity_is_carried_by_a_content_assertion_instead():
    """What the URL gave up, the extracted output asserts."""
    a = [CheckpointAssertion(output="member_name", contains_input="query")]

    ok, _ = assertions_met(a, {"member_name": "Hopper, Grace"}, {"query": "Hopper"})
    assert ok

    ok, reason = assertions_met(a, {"member_name": "Turing, Alan"}, {"query": "Hopper"})
    assert not ok and "Hopper" in reason


def test_an_assertion_is_derived_only_when_it_was_true_of_the_run():
    """Nothing invented: a claim the recorded run would itself have failed is
    a bug being written into the contract, not a property of the capability."""
    derived = _derive_assertions(
        {"member_name": "Hopper, Grace", "address": "1 Main St"},
        {"query": "Hopper", "search_by": "name"},
    )
    assert [(d.output, d.contains_input) for d in derived] == [("member_name", "query")]

    assert _derive_assertions(
        {"member_name": "Hopper, Grace"}, {"query": "Turing"}
    ) == []


def test_a_missing_output_or_param_fails_rather_than_skipping():
    """An assertion that quietly does not run is worse than none, because the
    artifact still advertises that it makes it."""
    a = [CheckpointAssertion(output="member_name", contains_input="query")]
    ok, reason = assertions_met(a, {}, {"query": "Hopper"})
    assert not ok and "not extracted" in reason
    ok, reason = assertions_met(a, {"member_name": "Hopper, Grace"}, {})
    assert not ok and "not supplied" in reason


def test_checkpoints_without_a_wildcard_are_untouched():
    """Every artifact recorded before this change compares exactly as before."""
    assert checkpoint_met("/members/{member_id}", {"member_id": "100987"},
                          "https://h/members/100987")
    assert not checkpoint_met("/members/{member_id}", {"member_id": "100987"},
                              "https://h/members/103001")


def test_assertions_survive_a_redacted_output():
    """The reason the containment is recorded at capture time, not derived later.

    member_name is in sensitive_output_fields, so what reaches the log is
    "***REDACTED***". A distiller matching the caller's query against THAT
    finds nothing and silently emits a weaker checkpoint, with no indication
    why. loop.py therefore records which parameters the raw value contained --
    the parameter NAMES only, never the value -- and the distiller reads that.
    """
    from src.artifact.distill import _derive_assertions_from_hints

    events = [
        {
            "event": "output_marked",
            "name": "member_name",
            "value": "***REDACTED***",          # what redaction leaves behind
            "contains_params": ["query"],       # computed before redaction
        },
        {"event": "output_marked", "name": "address", "value": "1 Main St",
         "contains_params": []},
    ]

    derived = _derive_assertions_from_hints(
        events, ["member_name", "address"], {"query": "Hopper", "search_by": "name"}
    )
    assert [(d.output, d.contains_input) for d in derived] == [("member_name", "query")]


def test_the_hint_records_no_values_only_parameter_names():
    """Whatever redaction was protecting stays protected."""
    from src.artifact.distill import _derive_assertions_from_hints

    events = [{"event": "output_marked", "name": "member_name",
               "value": "***REDACTED***", "contains_params": ["query"]}]
    derived = _derive_assertions_from_hints(events, ["member_name"], {"query": "Hopper"})

    # The assertion names an input to compare against at replay time; it does
    # not carry the surname, and could not reconstruct the masked value.
    assert derived[0].contains_input == "query"
    assert derived[0].contains_literal is None


def test_a_run_without_hints_still_distills():
    """Runs recorded before the hint existed fall back to the logged value."""
    from src.artifact.distill import _derive_assertions

    assert _derive_assertions({"member_name": "Hopper, Grace"}, {"query": "Hopper"})


# ---------------------------------------------------------------------------
# What the dashboard's status word means
# ---------------------------------------------------------------------------


def test_a_guardrail_refusal_reads_as_denied_not_unknown():
    """A refusal is a decision, not an absence of one.

    Denied runs write no result.json, so they previously displayed as
    'unknown' -- burying the clearest evidence the policy engine exists in the
    least informative word available.
    """
    from src.capability_api.runs import _display_status

    events = [{"event": "replay_started"},
              {"event": "replay_denied", "reason": "missing required params"}]
    assert _display_status(None, events) == "denied"

    events = [{"event": "run_started"},
              {"event": "run_denied", "reason": "Origin not in the configured allowlist."}]
    assert _display_status(None, events) == "denied"


def test_escalated_and_recovered_are_display_overrides_on_success():
    """Which means the plain 'success' count undercounts completed runs -- a
    run that finished perfectly but needed a human shows as escalated."""
    from src.capability_api.runs import _display_status

    success = {"status": "success"}
    assert _display_status(success, [{"event": "replay_finished"}]) == "success"
    assert _display_status(
        success, [{"event": "intervention_created"}, {"event": "operator_handed_back"}]
    ) == "escalated"
    assert _display_status(success, [{"event": "recovery_applied"}]) == "recovered"


def test_a_real_failure_is_never_relabelled():
    """The overrides apply only to runs the engine considered successful."""
    from src.capability_api.runs import _display_status

    failed = {"status": "failure"}
    assert _display_status(failed, [{"event": "intervention_created"},
                                    {"event": "operator_handed_back"}]) == "failure"
    assert _display_status({"status": "business_outcome"},
                           [{"event": "recovery_applied"}]) == "business_outcome"


# ---------------------------------------------------------------------------
# A content assertion holds in a MODE, not always
# ---------------------------------------------------------------------------
#
# Observed live: member_inquiry_by_name, invoked with a member number, failed
# its own checkpoint on a page it had reached correctly --
#
#   extracted member_name='Turing, Alan' does not contain '100987'
#
# The claim was never wrong. "The name we found contains what you searched
# for" is true when searching by NAME and false when searching by NUMBER,
# because a name never contains a member number. It was recorded without the
# condition that made it true, so a different parameter than the one it names
# could invalidate it.


def test_an_assertion_out_of_its_mode_does_not_apply():
    from src.artifact.schema import CheckpointAssertion
    from src.replay.checkpoint import assertions_met

    by_name = [CheckpointAssertion(output="member_name", contains_input="query",
                                   when={"search_by": "name"})]

    ok, _ = assertions_met(by_name, {"member_name": "Turing, Alan"},
                           {"query": "Turing", "search_by": "name"})
    assert ok, "in its mode, a true claim must hold"

    ok, reason = assertions_met(by_name, {"member_name": "Turing, Alan"},
                                {"query": "Hopper", "search_by": "name"})
    assert not ok and "Hopper" in reason, "in its mode, it must still catch a wrong record"

    ok, _ = assertions_met(by_name, {"member_name": "Turing, Alan"},
                           {"query": "100987", "search_by": "number"})
    assert ok, "out of its mode, the claim does not apply"


def test_the_mode_comes_from_what_was_SELECTED_not_typed():
    """A parameter bound to a select chose how the capability was operating.
    A parameter bound to a type is a value the caller supplied.

    Conditioning on the selected ones is what stops the assertion
    over-constraining to the exact inputs it was recorded with -- condition on
    the typed ones too and the capability only ever replays for Turing.
    """
    from src.artifact.distill import _mode_params
    from src.artifact.schema import ArtifactStep, LocatorCandidate

    def step(sid, action, ref):
        return ArtifactStep(
            step_id=sid, action=action, target_name="x",
            target=[LocatorCandidate(strategy="role_name", value="x", confidence=0.9)],
            input_ref=ref, description="d",
        )

    steps = [
        step("s1", "select", "search_by"),   # mode
        step("s2", "type", "query"),         # value
    ]
    modes = _mode_params(steps, {"search_by": "name", "query": "Hopper"})

    assert modes == {"search_by": "name"}
    assert "query" not in modes


def test_an_empty_mode_means_the_claim_always_holds():
    """Capabilities with no select keep an unconditional assertion, which is
    correct: there is no mode for it to depend on."""
    from src.artifact.schema import CheckpointAssertion
    from src.replay.checkpoint import assertions_met

    always = [CheckpointAssertion(output="member_name", contains_input="query")]
    ok, _ = assertions_met(always, {"member_name": "Hopper, Grace"}, {"query": "Hopper"})
    assert ok
    ok, _ = assertions_met(always, {"member_name": "Turing, Alan"}, {"query": "Hopper"})
    assert not ok
