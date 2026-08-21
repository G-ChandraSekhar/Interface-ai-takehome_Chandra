"""
Run history, read from the evidence directory.

The dashboard needs to show what the system did: which capabilities exist,
which runs happened, what each one returned, and why it stopped. All of that
already exists on disk -- `EvidenceWriter` has been writing `log.jsonl`,
`result.json`, and screenshots since the take-home. This module reads that,
and adds nothing to what the engine records.

Deliberately a reader over the existing evidence rather than a database.
Two reasons. A run's evidence bundle is the artifact a reviewer or auditor
would actually be handed, so the dashboard showing anything else would mean
the dashboard and the audit trail could disagree. And a second store would
need writing at every exit point of the replay engine -- the same fifteen
return sites that made telemetry a wrapper rather than an emission.

Status vocabulary shown to a person is richer than ReplayStatus, because
two things a reader cares about are properties of the run rather than of its
result: whether a human was pulled in, and whether the run healed itself.
Both are recoverable from the event log.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


def evidence_root() -> Path:
    override = os.environ.get("EVIDENCE_ROOT")
    return Path(override) if override else (REPO_ROOT / "evidence")


def _read_jsonl(path: Path) -> List[dict]:
    """Tolerates a partial trailing line -- a run killed mid-write should
    still be readable, and a dashboard that 500s on one bad byte is worse
    than one that shows the rest."""
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_ARTIFACT_TARGETS: dict = {}


def _normalize_target(target: Optional[str]) -> Optional[str]:
    """One name per console.

    Artifacts recorded before targets existed declare tenant "a"/"b"; both
    are the mock app, which src/targets.py already resolves the same way.
    Without this the run list would offer three filters for two consoles.
    """
    if target is None:
        return None
    from src.targets import _LEGACY_TENANT_TO_TARGET

    return _LEGACY_TENANT_TO_TARGET.get(target, target)


def _target_from_artifact(artifact_id, version) -> Optional[str]:
    """The target an artifact declares, cached across a listing."""
    if not artifact_id:
        return None
    key = str(artifact_id)
    if key in _ARTIFACT_TARGETS:
        return _ARTIFACT_TARGETS[key]

    target = None
    artifacts_dir = REPO_ROOT / "artifacts"
    if artifacts_dir.exists():
        for path in artifacts_dir.glob(str(artifact_id) + "@*.json"):
            data = _read_json(path) or {}
            target = (data.get("target") or {}).get("tenant")
            if target:
                break
    _ARTIFACT_TARGETS[key] = _normalize_target(target)
    return _ARTIFACT_TARGETS[key]


def _kind_of(dir_name: str) -> str:
    if dir_name.startswith("discovery_"):
        return "discovery"
    if dir_name.startswith("replay_") or dir_name.startswith("stability"):
        return "replay"
    return "other"


def _duration_ms(events: List[dict]) -> Optional[int]:
    stamps = [e["ts"] for e in events if e.get("ts")]
    if len(stamps) < 2:
        return None
    from datetime import datetime

    try:
        start = datetime.fromisoformat(stamps[0])
        end = datetime.fromisoformat(stamps[-1])
        return int((end - start).total_seconds() * 1000)
    except Exception:
        return None


def _escalation_windows(events: List[dict]) -> List[dict]:
    """Every stretch of wall-clock time a human held the session.

    This is the number the dashboard exists to make visible. A run that
    paused for ninety seconds because a person had to look at a screen and
    decide is not a slow run -- it is the system working as designed, and
    the gap is the evidence that a human was actually there.
    """
    from datetime import datetime

    windows = []
    pending = None
    took = None

    for e in events:
        name = e.get("event")
        ts = e.get("ts")
        if not ts:
            continue
        if name == "intervention_created":
            pending = e
        elif name == "operator_took_control":
            took = e
        elif name == "operator_handed_back" and pending is not None:
            try:
                requested_at = datetime.fromisoformat(pending["ts"])
                returned_at = datetime.fromisoformat(ts)
                waited = (
                    (datetime.fromisoformat(took["ts"]) - requested_at).total_seconds()
                    if took
                    else None
                )
                windows.append(
                    {
                        "reason": pending.get("reason"),
                        "step_id": pending.get("step_id"),
                        "page_url": pending.get("page_url"),
                        "requested_at": pending["ts"],
                        "returned_at": ts,
                        "seconds_until_taken": round(waited, 1) if waited is not None else None,
                        "seconds_total": round(
                            (returned_at - requested_at).total_seconds(), 1
                        ),
                        "human_actions": e.get("human_actions") or [],
                    }
                )
            except Exception:
                pass
            pending = None
            took = None

    return windows


def _display_status(result: Optional[dict], events: List[dict]) -> str:
    """The status a person reads, which is not always the status the engine
    returned.

    `escalated` and `recovered` are properties of how the run REACHED its
    result rather than of the result itself, and both are the first thing a
    reviewer scans for. Note this means the plain `success` count undercounts
    completed runs: a run that finished perfectly but needed a human displays
    as escalated.

    `denied` is the guardrails refusing before the run began -- an
    off-allowlist origin, a missing required parameter. Those write no
    result.json, so they previously showed as `unknown`, which buried the
    clearest evidence the policy engine exists in the least informative word
    available. A refusal is a decision, not an absence of one.
    """
    names = {e.get("event") for e in events}

    if "replay_denied" in names or "run_denied" in names:
        return "denied"

    status = (result or {}).get("status") or "unknown"

    if status == "success" and "intervention_created" in names:
        return "escalated"
    if status == "success" and "recovery_applied" in names:
        return "recovered"
    return status


def summarize_run(run_dir: Path) -> Optional[dict]:
    events = _read_jsonl(run_dir / "log.jsonl")
    result = _read_json(run_dir / "result.json")
    if not events and result is None:
        return None

    started = next((e for e in events if e.get("event") in ("run_started", "replay_started")), {})
    # Captured at the same moments as screenshots, so read the same way:
    # from the run's own directory, not by reconstructing a path.
    doms = sorted(p.name for p in (run_dir / "dom").glob("*.html")) if (
        run_dir / "dom"
    ).is_dir() else []

    shots = sorted(p.name for p in (run_dir / "screenshots").glob("*.png")) if (
        run_dir / "screenshots"
    ).exists() else []

    # Which target this run was against. Older runs predate targets and
    # only recorded a base_url, so it is derived rather than required --
    # evidence written before a field existed still has to be readable.
    target = started.get("target")
    if not target:
        base = started.get("base_url") or ""
        if base:
            target = "meridian" if "interface-hiring.com" in base else "mock"
    if not target:
        # Runs recorded before replay_started carried a target. The artifact
        # it replayed still knows, so resolve through that rather than
        # dropping the run from every filtered view -- replays are the whole
        # point of the system and should not be the ones that go missing.
        target = _target_from_artifact(started.get("artifact_id"), started.get("version"))
    target = _normalize_target(target)

    return {
        "run_id": run_dir.name,
        "kind": _kind_of(run_dir.name),
        "started_at": events[0]["ts"] if events and events[0].get("ts") else None,
        "duration_ms": _duration_ms(events),
        "status": _display_status(result, events),
        "engine_status": (result or {}).get("status"),
        "capability": started.get("artifact_id") or started.get("goal"),
        "artifact_version": started.get("version"),
        "target": target,
        "base_url": started.get("base_url"),
        "params": started.get("params") or {},
        "outputs": (result or {}).get("outputs") or {},
        "outcome_code": (result or {}).get("outcome_code"),
        "outcome_message": (result or {}).get("outcome_message"),
        "failure": (result or {}).get("failure"),
        "step_telemetry": (result or {}).get("step_telemetry") or [],
        "escalations": _escalation_windows(events),
        "screenshots": shots,
        # Paired with screenshots because they are captured together: the
        # picture shows what a person would have seen, the markup shows what
        # the locator ladder was resolving against.
        "dom_snapshots": doms,
        "event_count": len(events),
    }


def list_runs(
    limit: int = 200,
    kind: Optional[str] = None,
    target: Optional[str] = None,
) -> List[dict]:
    """Newest first. Directory names carry a UTC timestamp, so sorting the
    names sorts chronologically without opening anything."""
    root = evidence_root()
    if not root.exists():
        return []

    runs = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if kind and _kind_of(d.name) != kind:
            continue
        summary = summarize_run(d)
        if summary is None:
            continue
        if target and summary.get("target") != target:
            continue
        runs.append(summary)
        if len(runs) >= limit:
            break
    return runs


def run_detail(run_id: str) -> Optional[dict]:
    """One run, with its full event log.

    `run_id` is resolved against the evidence root and rejected if it
    escapes -- it arrives from a URL path, and a dashboard that will read
    any file the process can reach is a dashboard that will be asked to.
    """
    root = evidence_root().resolve()
    run_dir = (root / run_id).resolve()
    if not str(run_dir).startswith(str(root)) or not run_dir.is_dir():
        return None

    summary = summarize_run(run_dir)
    if summary is None:
        return None
    summary["events"] = _read_jsonl(run_dir / "log.jsonl")
    return summary


def dom_path(run_id: str, name: str) -> Optional[Path]:
    """Resolve a DOM snapshot inside a run's bundle, refusing traversal."""
    root = evidence_root() / run_id / "dom"
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def screenshot_path(run_id: str, name: str) -> Optional[Path]:
    root = evidence_root().resolve()
    path = (root / run_id / "screenshots" / name).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        return None
    return path
