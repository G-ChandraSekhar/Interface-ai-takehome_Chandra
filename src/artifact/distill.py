"""
Distiller: transcript -> capability artifact.

Reads a discovery run's log.jsonl (written by src/discovery/evidence.py) and
produces a strict, validated Artifact. This is the boundary between "what
the model did on one run" and "what any caller can invoke going forward" --
nothing from the raw transcript survives except the ordered steps, their
locator ladders, and which values were parameters vs fixed.

Parameterization rule: a 'type' step's text is recorded as `input_ref` when
it exactly matches one of the *invocation* parameter values the run was
started with (e.g. member_id="4521"), and as `literal_value` otherwise. This
is deliberately simple and deterministic rather than guessing intent --
matching the reference architecture's "deterministic parameter binding
rather than a second LLM metadata pass" choice, for the same reason: a
human reviewer needs to be able to verify the binding by inspection.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from src.artifact.schema import (
    Artifact,
    ArtifactDetectors,
    ArtifactPolicy,
    ArtifactStep,
    Checkpoint,
    DetectorPattern,
    ExtractionRule,
    LocatorCandidate,
    ParamSpec,
    TargetSpec,
)
from src.replay.detectors import (
    _DEFAULT_BUSINESS_OUTCOME_MARKERS,
    _DEFAULT_HARD_FAILURE_MARKERS,
    _DEFAULT_RECOVERABLE_MARKERS,
)

_TENANT_BY_ROUTE_PREFIX = {"/desk": "a", "/operations": "b"}

_ACTIONABLE_TOOLS = {"click", "type", "select"}


def _default_detectors_for_tenant(tenant: str) -> ArtifactDetectors:
    """Snapshot replay/detectors.py's built-in defaults into an explicit,
    reviewable artifact field at distill time.

    Discovery never observes error states -- distill_run() only ever runs
    against a log whose run_finished.status == "success" (enforced below),
    so there is nothing to *capture* the way locator candidates or
    extraction labels are captured from what the run actually did. What we
    CAN do is make the currently-implicit, code-level defaults explicit on
    every new artifact, so a reviewer approving this artifact is also
    reviewing what counts as e.g. "not found" for it, and so a different
    vendor app's copy can be substituted here later without anyone editing
    detectors.py. Both current tenants render identical error copy (they
    share templates -- see tenants.py's own docstring: "the *same*
    underlying vendor product"), so this returns the same set for either
    tenant today; a real second vendor app would get its own entry here.
    """
    return ArtifactDetectors(
        business_outcomes=[
            DetectorPattern(marker=marker, code=code, message=message)
            for marker, code, message in _DEFAULT_BUSINESS_OUTCOME_MARKERS
        ],
        recoverable=[
            DetectorPattern(marker=marker, code=condition, message="")
            for marker, condition in _DEFAULT_RECOVERABLE_MARKERS
        ],
        hard_failures=[
            DetectorPattern(marker=marker, code=failure_class.value, message="")
            for marker, failure_class in _DEFAULT_HARD_FAILURE_MARKERS
        ],
    )

# Fixed per-strategy priors, not measured probabilities -- they encode how
# tightly each locator kind is coupled to things that change. An accessible
# role+name survives most markup edits; a name= attribute usually does; an
# id is stable unless it's generated; visible text moves with copy edits;
# a positional marker is a recorded admission that nothing stable existed.
_STRATEGY_CONFIDENCE = {
    "role_name": 0.9,
    # Above the CSS attribute deliberately: the visible label is what a
    # human reads and tends to stay constant across tenants running the same
    # vendor product, whereas the underlying name= attribute is an
    # implementation detail that varies (demonstrated concretely by this
    # project's own Tenant B overlay).
    "label_proximity": 0.85,
    "css_name_attr": 0.75,
    "css_id": 0.7,
    "text": 0.55,
    "positional": 0.2,
}


class DistillationError(ValueError):
    pass


def _read_events(log_path: Path) -> list[dict]:
    events = []
    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _build_step(event: dict, params: dict[str, str], step_id: str) -> ArtifactStep:
    tool_name = event["tool_name"]
    target_name = event.get("target_name")
    raw_candidates = event.get("target_candidates") or []
    candidates = [
        LocatorCandidate(
            strategy=c["strategy"],
            value=c["value"],
            confidence=_STRATEGY_CONFIDENCE.get(c["strategy"], 0.5),
        )
        for c in raw_candidates
    ]
    if not candidates:
        candidates = [
            LocatorCandidate(
                strategy="positional",
                value="no stable locator recorded",
                confidence=_STRATEGY_CONFIDENCE["positional"],
            )
        ]

    input_ref = None
    literal_value = None
    if tool_name == "type":
        text = (event.get("tool_args") or {}).get("text", "")
        for pname, pval in params.items():
            if str(pval) == str(text):
                input_ref = pname
                break
        if input_ref is None:
            literal_value = text
    elif tool_name == "select":
        literal_value = (event.get("tool_args") or {}).get("option_text")

    description = f"{tool_name} '{target_name}'" if target_name else tool_name

    return ArtifactStep(
        step_id=step_id,
        action=tool_name,
        target_name=target_name,
        target=candidates,
        input_ref=input_ref,
        literal_value=literal_value,
        description=description,
    )


def _url_pattern_from(url: str, params: dict[str, str]) -> str:
    path = urlparse(url).path
    for pname, pval in params.items():
        if pval and str(pval) in path:
            path = path.replace(str(pval), "{" + pname + "}")
    return path


def distill_run(
    log_path: Path,
    *,
    artifact_id: str,
    name: str,
    params: dict[str, str],
    required_outputs: list[str],
    version: int = 1,
) -> Artifact:
    events = _read_events(log_path)
    if not events:
        raise DistillationError("No events found in " + str(log_path))

    run_started = next((e for e in events if e["event"] == "run_started"), None)
    if run_started is None:
        raise DistillationError("Log has no run_started event")

    run_finished = next((e for e in events if e["event"] == "run_finished"), None)
    if run_finished is None or run_finished.get("status") != "success":
        raise DistillationError(
            "Refusing to distill a run that did not finish with status=success. "
            "Only successful discovery runs may become artifacts."
        )

    base_url = run_started["base_url"]
    route_prefix = run_started["route_prefix"]
    goal = run_started["goal"]
    tenant = _TENANT_BY_ROUTE_PREFIX.get(route_prefix, "unknown")

    steps = []
    step_events = [
        e
        for e in events
        if e["event"] == "step" and e.get("tool_ok") and e.get("tool_name") in _ACTIONABLE_TOOLS
    ]
    for i, event in enumerate(step_events):
        step = _build_step(event, params, step_id="s" + str(len(steps) + 1))
        if step.action == "click":
            # loop.py logs page_url AFTER executing the action (see
            # src/discovery/loop.py's evidence.log_step call, which runs
            # after execute_tool) -- so a click step's OWN event.page_url
            # already IS its destination. No forward-scan needed; using a
            # later event's page_url here was a real bug (it attributed the
            # NEXT step's destination to THIS step, which happened to be
            # harmless only because both steps' paths carried the same
            # policy risk tier in this particular artifact).
            step.target_url = event.get("page_url")
        steps.append(step)

    if not steps:
        raise DistillationError("No actionable steps found in log -- nothing to distill.")

    output_events = [e for e in events if e["event"] == "output_marked"]
    marked_output_names = {e["name"] for e in output_events}
    missing = set(required_outputs) - marked_output_names
    if missing:
        raise DistillationError(
            "Required outputs " + str(missing) + " were never marked in this run -- cannot distill."
        )

    output_schema = {name: ParamSpec(type="str", required=True) for name in required_outputs}
    input_params = {name: ParamSpec(type="str", required=True) for name in params}

    # Build the extraction rule for each required output from the label
    # captured at mark_output time. Missing a label here means replay would
    # have no reliable way to re-find that value on a different page -- we
    # refuse to distill rather than silently fall back to replaying the
    # frozen discovery-time value, which would defeat parameterization.
    output_extraction: dict[str, ExtractionRule] = {}
    no_label_for: list[str] = []
    for output_name in required_outputs:
        matching_events = [e for e in output_events if e["name"] == output_name]
        label = matching_events[-1].get("extraction_label") if matching_events else None
        if not label:
            no_label_for.append(output_name)
            continue
        output_extraction[output_name] = ExtractionRule(strategy="table_row_label", label=label)

    if no_label_for:
        raise DistillationError(
            "Could not determine a reliable extraction label for output(s) "
            + str(no_label_for)
            + " -- refusing to distill an artifact that can't re-extract its own outputs."
        )

    last_output_event = output_events[-1]
    checkpoint_url = last_output_event.get("page_url", "")
    checkpoint = Checkpoint(
        description="All required outputs (" + ", ".join(required_outputs) + ") are visible on this page.",
        url_pattern=_url_pattern_from(checkpoint_url, params),
    )

    run_dir_name = log_path.parent.name
    created_from_run_id = run_dir_name[len("discovery_"):] if run_dir_name.startswith("discovery_") else run_dir_name

    return Artifact(
        artifact_id=artifact_id,
        name=name,
        version=version,
        goal=goal,
        target=TargetSpec(tenant=tenant, base_url=base_url, route_prefix=route_prefix),
        input_params=input_params,
        output_schema=output_schema,
        steps=steps,
        checkpoint=checkpoint,
        output_extraction=output_extraction,
        # The artifact's own policy, derived from what this run actually
        # did -- the single origin it touched and only the action kinds it
        # actually used. Deliberately narrow: a lookup capability that only
        # ever typed and clicked has no business being able to navigate
        # arbitrarily later, even if the global policy would permit it.
        policy=ArtifactPolicy(
            allowed_origins=[base_url],
            allowed_actions=sorted({step.action for step in steps}),
        ),
        detectors=_default_detectors_for_tenant(tenant),
        created_from_run_id=created_from_run_id,
        created_at=run_started["ts"],
        approved=False,
    )