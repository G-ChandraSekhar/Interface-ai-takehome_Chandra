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
import re
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
    detectors_from_target,
)

_TENANT_BY_ROUTE_PREFIX = {"/desk": "a", "/operations": "b"}

_ACTIONABLE_TOOLS = {"click", "type", "select"}


def _default_detectors_for_tenant(tenant: str) -> ArtifactDetectors:
    """Snapshot the target's declared detector taxonomy onto a new artifact.

    Discovery never observes error states -- distill_run() only ever runs
    against a log whose run_finished.status == "success" (enforced below),
    so there is nothing to *capture* the way locator candidates or
    extraction labels are captured from what the run actually did.

    What we can do is make the taxonomy explicit on every new artifact, so
    a reviewer approving this capability is also approving what counts as
    "not found", "session expired", or "the host is broken" for it. It is
    read from config/targets/<tenant>.yaml, so a new console needs a YAML
    file rather than an edit to src/replay/detectors.py -- and once written
    into the artifact, that artifact keeps classifying the way its reviewer
    saw it even if the target config later changes underneath it.

    Falls back to detectors.py's module constants for a target with no
    config, which is what keeps previously-distilled artifacts working.
    """
    from_target = detectors_from_target(tenant)
    if from_target is not None:
        return from_target

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
        # Same binding rule as 'type', and for the same reason. This used to
        # freeze every dropdown choice as a literal, which was harmless when
        # the only recorded select was "always pick Holiday Savings" -- and
        # wrong the moment a dropdown carries a real parameter. A funds
        # transfer whose from-share and to-share are frozen is a capability
        # that can only ever move money between the same two accounts.
        chosen = (event.get("tool_args") or {}).get("option_text")
        for pname, pval in params.items():
            if chosen is not None and str(pval) == str(chosen):
                input_ref = pname
                break
        if input_ref is None:
            literal_value = chosen

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
    # Newer runs record which target config drove them. Older runs predate
    # targets entirely and are always the mock app, where the route prefix
    # identifies the tenant unambiguously -- so that stays the fallback
    # rather than a guess.
    tenant = run_started.get("target") or _TENANT_BY_ROUTE_PREFIX.get(route_prefix, "unknown")

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
        latest = matching_events[-1] if matching_events else {}

        # Newer runs log the whole locator dict (extract.locate_value);
        # older logs only carry a bare label, which always meant a
        # label/value row. Both are honoured so previously-captured runs
        # stay distillable.
        located = latest.get("extraction_rule")
        if not located:
            label = latest.get("extraction_label")
            located = {"strategy": "table_row_label", "label": label} if label else None

        if not located or not located.get("label"):
            no_label_for.append(output_name)
            continue

        if located["strategy"] == "table_grid_cell":
            # Bind the row key the same way a 'type' step's text is bound:
            # a key matching an invocation parameter becomes a reference
            # that varies per call, anything else is frozen into the flow.
            key_value = located.get("key_value")
            key_input_ref = next(
                (p for p, v in params.items() if str(v) == str(key_value)), None
            )
            output_extraction[output_name] = ExtractionRule(
                strategy="table_grid_cell",
                label=located["label"],
                key_column=located.get("key_column"),
                key_input_ref=key_input_ref,
                key_literal=None if key_input_ref else key_value,
            )
        else:
            output_extraction[output_name] = ExtractionRule(
                strategy="table_row_label", label=located["label"]
            )

    if no_label_for:
        raise DistillationError(
            "Could not determine a reliable extraction label for output(s) "
            + str(no_label_for)
            + " -- refusing to distill an artifact that can't re-extract its own outputs."
        )

    # Every output must have been marked on the SAME page: that page becomes
    # the checkpoint, and replay re-derives all of the outputs from it. An
    # output marked on a page the flow later navigated away from produces an
    # extraction rule that cannot resolve at replay time -- an artifact that
    # validates, distills, and then fails on its first invocation.
    #
    # Caught by exactly that happening: a run marked both outputs on the
    # search-results list rather than on the member record it went on to
    # open. The prompt asked for early marking, which was harmless on a flow
    # whose outputs only ever appeared on its final page and wrong the
    # moment one didn't.
    output_pages = {e.get("page_url", "") for e in output_events if e["name"] in required_outputs}
    if len(output_pages) > 1:
        raise DistillationError(
            "Outputs were marked on different pages "
            + str(sorted(output_pages))
            + " -- every output must be readable from the single page the "
            "capability ends on, or replay cannot re-extract them. Re-run "
            "discovery with a goal that states where the values must be read."
        )

    last_output_event = output_events[-1]
    checkpoint_url = last_output_event.get("page_url", "")
    checkpoint = Checkpoint(
        description="All required outputs (" + ", ".join(required_outputs) + ") are visible on this page.",
        url_pattern=_url_pattern_from(checkpoint_url, params),
    )

    # Every declared input must actually be consumed, or the capability
    # advertises a setting that does nothing.
    #
    # This is not a style rule. The catalogue publishes input_params as the
    # arguments a caller may set; replay only ever varies a value some step
    # references. An input nothing references is a promise the artifact
    # cannot keep -- a caller asks to freeze share A for reason X, the run
    # succeeds, returns a confirmation number, and freezes share B for
    # reason Y instead. Every signal reports success and the evidence bundle
    # agrees with it.
    #
    # This has happened here: a Place Account Hold recording where the model
    # left both dropdowns on their pre-selected defaults, producing an
    # artifact that could only ever freeze the first share for fraud while
    # declaring otherwise. It was caught by reading the steps, which is not
    # a control.
    #
    # Refusing rather than warning, deliberately. The failure this prevents
    # is silent and lands on a customer; the failure it can cause is loud,
    # lands on a developer, and is fixed by re-recording. Those costs are
    # not comparable.
    referenced = {step.input_ref for step in steps if step.input_ref}
    referenced |= {
        rule.key_input_ref
        for rule in output_extraction.values()
        if getattr(rule, "key_input_ref", None)
    }
    # A checkpoint pattern is rendered with the invocation's params, so a
    # param used only there is genuinely consumed even though no step names it.
    referenced |= set(re.findall(r"\{(\w+)\}", checkpoint.url_pattern))

    unused = sorted(set(input_params) - referenced)
    if unused:
        raise DistillationError(
            "Input param(s) "
            + str(unused)
            + " are declared but no step, extraction rule, or checkpoint uses "
            "them -- the capability would advertise settings that do nothing, "
            "and a caller changing them would get a successful run that "
            "ignored their values. Re-record with a goal that sets every "
            "field explicitly, even where a value already appears selected."
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