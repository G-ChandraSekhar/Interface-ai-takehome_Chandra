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
    ArtifactStep,
    Checkpoint,
    LocatorCandidate,
    ParamSpec,
    TargetSpec,
)

_TENANT_BY_ROUTE_PREFIX = {"/desk": "a", "/operations": "b"}

_ACTIONABLE_TOOLS = {"click", "type", "select"}


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
    candidates = [LocatorCandidate(**c) for c in raw_candidates]
    if not candidates:
        candidates = [
            LocatorCandidate(strategy="positional", value="no stable locator recorded")
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
    for event in events:
        if event["event"] != "step":
            continue
        if not event.get("tool_ok"):
            continue
        if event.get("tool_name") not in _ACTIONABLE_TOOLS:
            continue
        steps.append(_build_step(event, params, step_id="s" + str(len(steps) + 1)))

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
        created_from_run_id=created_from_run_id,
        created_at=run_started["ts"],
        approved=False,
    )
