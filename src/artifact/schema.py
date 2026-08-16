"""
Artifact schema.

This is the "focal point of the evaluation" per the assignment brief: a
capability contract, not a transcript. A human reviewer and a calling AI
agent both need to understand, from this file alone, what the capability
does, what it needs, and what it returns -- without ever looking at the
discovery run that produced it.

Design choices, and why:

- `target` records exactly what the artifact is scoped to (tenant, origin,
  route prefix). Replay checks this against the guardrails allowlist before
  doing anything -- an artifact can't be pointed at a different origin than
  the one it was discovered against.
- Every step's `target` is a *ranked locator ladder*, not a single selector.
  This is what makes replay robust to the kind of drift a real legacy app
  accumulates over time: if the top-ranked locator stops resolving, replay
  falls back down the ladder and reports which tier it needed (Phase 4).
- `input_ref` vs `literal_value` on a step distinguishes "this value came
  from the caller's parameters" from "this value is a fixed part of the
  flow" (e.g. always selecting the same dropdown option). Only input_ref
  values are safe to vary per invocation; literal_value ones are frozen.
- `checkpoint` is a URL pattern, not a page-content assertion, deliberately:
  it's cheap to check, doesn't require re-parsing page text, and is exactly
  what proves the flow actually reached the state it claims to have reached.
- The whole model is strict (`extra="forbid"`) so there's no possible field
  for a screenshot, raw transcript, or credential to hide in -- if it isn't
  declared here, it cannot be part of an artifact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class LocatorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["role_name", "css_name_attr", "css_id", "text", "positional"]
    value: str
    # How much this strategy is trusted to still identify the same element
    # later. Not a probability -- a fixed per-strategy prior reflecting how
    # tightly the locator is coupled to things that change: an accessible
    # role+name survives most markup edits, a name= attribute usually does,
    # visible text changes with copy edits, and a positional marker is a
    # recorded admission that nothing stable was found. Makes the drift
    # signal quantitative: a step resolving at confidence 0.4 is one markup
    # change from breaking, even while it still passes.
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["str", "int", "decimal"] = "str"
    required: bool = True
    description: str = ""


class TargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    base_url: str
    route_prefix: str


class ArtifactStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    action: Literal["navigate", "click", "type", "select", "wait_for", "read_text"]
    target_name: Optional[str] = None
    target: list[LocatorCandidate] = Field(default_factory=list)
    # For a click that navigates: the URL it led to during discovery. Lets
    # replay's guardrails check apply to the actual destination, the same
    # way Phase 2's discovery tools.py checks a click by destination rather
    # than by the (possibly safe) page it's clicked from.
    target_url: Optional[str] = None
    # exactly one of these should be set for a 'type' or 'select' step;
    # both are None for 'click'/'navigate'/'wait_for'/'read_text'
    input_ref: Optional[str] = None
    literal_value: Optional[str] = None
    description: str = ""


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    url_pattern: str  # path with param values replaced by {param_name}


class ExtractionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # table_row_label: the final page renders "Label\tValue" rows (this is
    # exactly what Playwright's own inner_text() produces for the legacy
    # table markup in this target app); replay finds the row whose label
    # matches and returns that row's value. This is re-resolved against
    # whatever page replay actually lands on -- so a different invocation
    # parameter (a different member_id) naturally yields a different real
    # value, rather than replaying back the value frozen at discovery time.
    strategy: Literal["table_row_label"]
    label: str


class ArtifactPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The artifact's OWN allowlist, checked in addition to the global
    # operator policy in config/allowlist.yaml -- not instead of it. Both
    # must permit an action for it to run. This is defense in depth with a
    # specific purpose: the global policy is what the operator permits in
    # general, while this is what THIS capability's reviewer approved when
    # they signed it off. A capability can therefore never quietly widen
    # its own reach if the global policy is later loosened for some other
    # capability's sake.
    allowed_origins: list[str]
    allowed_actions: list[
        Literal["navigate", "click", "type", "select", "wait_for", "read_text"]
    ]


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    name: str
    version: int = 1
    goal: str
    target: TargetSpec
    input_params: dict[str, ParamSpec]
    output_schema: dict[str, ParamSpec]
    steps: list[ArtifactStep]
    checkpoint: Checkpoint
    # how to re-derive each declared output from whatever page replay
    # actually ends on -- required for every key in output_schema
    output_extraction: dict[str, ExtractionRule]
    # Optional so artifacts distilled before this field existed still load;
    # when present, replay enforces it on top of the global policy.
    policy: Optional[ArtifactPolicy] = None
    created_from_run_id: str
    created_at: datetime
    approved: bool = False
