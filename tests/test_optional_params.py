"""
Optional input parameters.

A capability that writes three fields was demanding all three from anyone who
wanted to change one -- and typing an empty string into the other two would
have BLANKED them. Refusing to run was honest, and it made correcting a phone
number cost the caller a member's e-mail and address.

An optional parameter means "leave that field as it is": replay skips the step
that would fill it. The safety of the change does not come from the parameter
being required, it comes from the tier -- a mutating action still needs an
explicit confirmation showing exactly what will change, and an irreversible one
still needs a human at the live session.
"""

from __future__ import annotations

import inspect

import pytest

from src.artifact.distill import DistillationError


def test_a_param_is_required_unless_named_optional():
    """The default stays strict; you opt out per parameter."""
    source = inspect.getsource(
        __import__("src.artifact.distill", fromlist=["x"]).distill_run
    )
    assert "required=name not in optional" in source


def test_marking_an_unknown_param_optional_is_refused():
    """A typo would otherwise silently leave every field required, and the
    caller would only find out when replay demanded one they thought was
    optional."""
    from src.artifact import distill

    source = inspect.getsource(distill.distill_run)
    assert "Marked optional but not an input param" in source


def test_only_value_steps_are_skipped_never_navigation():
    """A click is navigation. Skipping one would silently take the flow
    somewhere the artifact never described -- which is a far worse failure
    than asking for one more argument."""
    from src.replay import engine

    source = inspect.getsource(engine._execute_replay)
    # The guard itself, not the comment above it.
    assert 'step.action in ("type", "select")' in source
    guard = source.index('step.action in ("type", "select")')
    skip = source.index('"step_skipped"')
    assert guard < skip, "the action check must gate the skip, not follow it"
    assert "click" not in source[guard:skip]


def test_a_skip_is_recorded_in_the_evidence():
    """A field that was deliberately left alone and a field that was missed
    look identical afterwards unless the run says which it was."""
    from src.replay import engine

    source = inspect.getsource(engine._execute_replay)
    assert '"step_skipped"' in source
    assert "field=step.target_name" in source


def test_the_catalog_only_demands_required_params():
    """So the chatbot stops asking for what the caller may omit -- it reads
    the same required list an agent would."""
    from src.artifact.schema import ParamSpec
    from src.capability_api.registry import artifact_to_tool_schema
    from src.artifact.store import load_artifact_by_id
    from pathlib import Path

    artifact = load_artifact_by_id("open_sub_account", 1, Path("artifacts"))
    artifact.input_params["member_id"] = ParamSpec(type="str", required=True)
    artifact.input_params["note"] = ParamSpec(type="str", required=False)

    schema = artifact_to_tool_schema(artifact)["function"]["parameters"]
    assert "note" in schema["properties"], "an optional param is still offered"
    assert "note" not in schema["required"], "but it is not demanded"
    assert "member_id" in schema["required"]


# ---------------------------------------------------------------------------
# The catalog publishes a description, never the recording goal
# ---------------------------------------------------------------------------


def test_the_goal_is_never_published_as_a_tool_description():
    """The fourth defect of the same class, and the most embarrassing.

    artifact.goal is what the MODEL was told at recording time. For
    update_member_information it read: "search by member number for 100987 ...
    explicitly set ALL THREE fields even if a value already appears there ...
    set the e-mail to alan.turing@bletchley.example".

    Published as the tool description, that is a recording transcript posing
    as an API contract. It names the member the recording happened to use and
    the values it happened to set -- and a model reads it as guidance for the
    CALLER and obeys it. Asked to change a phone number, it demanded an
    e-mail and an address that the schema right beside it said were optional.

    The schema was correct the whole time. The prose above it was not.
    """
    import inspect

    from src.capability_api import registry

    source = inspect.getsource(registry.artifact_to_tool_schema)
    assert "artifact.description or artifact.name" in source
    assert '"description": artifact.goal' not in source


def test_description_falls_back_to_name_not_goal():
    from src.artifact.schema import Artifact

    assert "description" in Artifact.model_fields
    assert Artifact.model_fields["description"].default is None
