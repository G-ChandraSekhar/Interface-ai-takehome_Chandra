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

    strategy: Literal[
        "role_name", "label_proximity", "css_name_attr", "css_id", "text", "positional"
    ]
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

    # What a valid value looks like, taken from the run this was recorded
    # from. Shown as a placeholder, never pre-filled: a form that pre-fills a
    # member number is a form that runs against the wrong member the moment
    # someone stops reading.
    #
    # Exists because a caller typed "S0070" into a share field whose options
    # read "100234-S0070". The contract said the parameter was a string, which
    # was true and useless.
    example: Optional[str] = None

    # The exact set of values the target accepts, for a parameter bound to a
    # dropdown. Published in the tool schema, where a model API ENFORCES it
    # rather than suggesting it.
    #
    # An example was not enough. `search_by` carried `example: "name"` and the
    # model still sent "last_name" -- a reasonable paraphrase of the visible
    # label "Last Name", and wrong. A hint invites interpretation; a closed set
    # does not.
    enum: Optional[list] = None

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


class CheckpointAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "the value extracted for `output` must contain this invocation's value
    # for `contains_input`" -- e.g. member_name must contain the surname the
    # caller searched for.
    #
    # Exists because a URL pattern can only assert a destination the caller's
    # own parameters can predict. Search by member number ends at
    # /members/{member_id}, which IS the input. Search by last name ends
    # wherever the search resolved, which the caller does not know and cannot
    # be asked for. Without a content claim the artifact either freezes one
    # member's URL (works for one surname) or wildcards it away (asserts
    # nothing about identity). This asserts identity in the terms the caller
    # actually supplied.
    output: str
    contains_input: Optional[str] = None
    contains_literal: Optional[str] = None
    case_sensitive: bool = False

    # The mode this claim holds in, as {param: value}. Empty means always.
    #
    # "member_name contains query" is true when searching by NAME and false
    # when searching by NUMBER -- a name never contains a member number. The
    # claim was never wrong; it was recorded without the condition that made
    # it true, so a different parameter than the one it names could
    # invalidate it. Observed live: a by-name capability invoked with a
    # member number failed its own checkpoint on a page it had reached
    # correctly.
    when: dict[str, str] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    # Path with param values replaced by {param_name}. A segment the
    # distiller could not bind to any parameter becomes {*}, which matches
    # exactly one segment -- NOT fnmatch's `*`, which crosses `/` and would
    # let a run that died mid-flow on /members/100987/transfer satisfy a
    # checkpoint of /members/*.
    #
    # A {*} segment weakens the URL claim to a shape, so anything that needs
    # to be said about WHICH record was reached is said in `assertions`.
    url_pattern: str
    assertions: list[CheckpointAssertion] = Field(default_factory=list)


class ExtractionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # table_row_label: the page renders "Label\tValue" pairs (what
    # Playwright's inner_text() produces for legacy table markup); replay
    # finds the pair whose label matches and returns its value. A dense
    # layout may put several pairs on one line -- see extract.py.
    #
    # table_grid_cell: the value lives in a data grid with a header row
    # (e.g. "Share ID | Type | Balance | Status"), where there is no
    # label/value pair at all and the question is two-dimensional: "the
    # Balance cell of the row whose Share ID is X". Needed the moment a
    # capability reads one row out of a repeating table -- a member's
    # per-share balance, a transaction line, a search result.
    #
    # Either way the rule is re-resolved against whatever page replay
    # actually lands on, so a different invocation parameter yields a
    # different real value rather than replaying back the value frozen at
    # discovery time.
    strategy: Literal["table_row_label", "table_grid_cell"]
    # row label (table_row_label) or value-column header (table_grid_cell)
    label: str

    # --- table_grid_cell only -------------------------------------------
    # Which column identifies the row, e.g. "Share ID".
    key_column: Optional[str] = None
    # Which row to read. Exactly as ArtifactStep distinguishes input_ref
    # from literal_value, and for the same reason: only a caller-supplied
    # key is safe to vary per invocation, a frozen one is part of the flow.
    key_input_ref: Optional[str] = None
    key_literal: Optional[str] = None


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How replay clears a recoverable condition itself.
    #   click_text     -- click the control with this visible text (a
    #                     "Continue" on a maintenance interstitial)
    #   reauthenticate -- the session is gone; sign on again via the
    #                     target's configured sign-on (src/targets.py)
    kind: Literal["click_text", "reauthenticate"]
    value: Optional[str] = None  # required for click_text

    # Re-navigate to the URL the flow was on before recovering.
    #
    # Not a convenience. On MERIDIAN both recoveries land the browser
    # somewhere else entirely -- the maintenance screen's "Continue" goes to
    # /menu, and a timeout drops to /signon -- so a recovery that clears the
    # interstitial and simply carries on would continue the flow against the
    # wrong page while reporting that it recovered. That is worse than not
    # recovering at all, because it fails silently instead of loudly. Left
    # False for targets whose interstitial returns you where you were.
    resume: bool = True


class DetectorPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Plain substring match against page text, deliberately -- same
    # reasoning as ExtractionRule.label: cheap, legible to a human reviewer,
    # and it's exactly the signal digest.py already showed the model during
    # discovery, so replay classifies on the same evidence a human approving
    # this artifact could read for themselves.
    marker: str
    # For business_outcomes: any string, becomes ReplayResult.outcome_code.
    # For recoverable: any string, becomes the recovery condition name.
    # For hard_failures: MUST match one of FailureClass's string values
    # (src/replay/result.py -- e.g. "app_error", "policy_denied") since
    # replay constructs FailureClass(code) directly from it.
    code: str
    message: str = ""  # human-facing message; unused for recoverable/hard_failure

    # The status the host returns with this page, recorded for evidence
    # rather than used to classify. Classification stays marker-driven
    # because status alone is not sufficient on a real target: MERIDIAN
    # answers a natural "no member records matched your search" with HTTP
    # 200, and returns 400 for both a legitimate insufficient-funds outcome
    # and an injected fault. Status corroborates; the page text decides.
    http_status: Optional[int] = None

    # Recoverable patterns only: what to do about it.
    recovery: Optional[RecoveryAction] = None


class ArtifactDetectors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Declared per-artifact rather than hardcoded in replay/detectors.py --
    # this is what lets the same three-way result contract (business
    # outcome / recoverable / hard failure) generalize across vendor apps
    # with different copy, without editing Python for every new tenant.
    # A reviewer approving this artifact is thereby also approving what
    # counts as e.g. "not found" for it -- the same review boundary
    # ArtifactPolicy already draws for permissions.
    business_outcomes: list[DetectorPattern] = Field(default_factory=list)
    recoverable: list[DetectorPattern] = Field(default_factory=list)
    hard_failures: list[DetectorPattern] = Field(default_factory=list)


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

class ArtifactStability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Written by `python3 -m src.cli stability ... --update-artifact`
    # (src/replay/stability.py's run_stability()) -- a decision-support
    # signal for whoever is deciding whether to flip Artifact.approved,
    # never something that flips it automatically. `approved` is
    # deliberately a human reviewer's out-of-band decision everywhere else
    # in this codebase (see guardrails/engine.py, capability_api/server.py)
    # -- a computed score informing that decision is not the same thing as
    # a script making it, and conflating the two would quietly remove the
    # human from a loop this system otherwise goes out of its way to keep
    # them in.
    sample_size: int
    success_rate: float = Field(ge=0.0, le=1.0)
    business_outcome_rate: float = Field(ge=0.0, le=1.0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    # step_id -> observed values across the sample. A step whose average
    # tier is climbing toward 2+ is drifting toward eventual failure even
    # while every individual sampled run still "passes" -- this is the
    # earliest available warning signal, well before success_rate itself
    # would show any decline.
    step_avg_tier: dict[str, float] = Field(default_factory=dict)
    step_worst_tier: dict[str, int] = Field(default_factory=dict)
    computed_at: datetime

class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    name: str
    version: int = 1
    # What the model was told at DISCOVERY time. A recording instruction --
    # "explicitly set all three fields", "search for member 100987" -- kept
    # because it is part of the provenance of this artifact.
    #
    # It is NOT a description of the capability, and must never be published
    # as one: it names the member the recording happened to use and the
    # values it happened to set, and it carries instructions aimed at the
    # model doing the recording rather than at anyone calling the result.
    goal: str

    # What the CATALOG publishes. Falls back to `name` when unset, because a
    # capability's name is a truthful if terse description and the goal is
    # not a description at all.
    description: Optional[str] = None
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
    # Optional for the same reason: artifacts distilled before this existed
    # fall back to replay/detectors.py's hardcoded defaults (Tenant A's
    # actual copy). When present, these patterns are used instead --
    # letting a new vendor app's error copy be declared and reviewed here
    # rather than requiring a code change to src/replay/detectors.py.
    detectors: Optional[ArtifactDetectors] = None
    # Optional, computed rather than authored -- see ArtifactStability's
    # own docstring for why this never touches `approved` on its own.
    stability: Optional[ArtifactStability] = None
    created_from_run_id: str
    created_at: datetime
    approved: bool = False