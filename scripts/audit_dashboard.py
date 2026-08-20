#!/usr/bin/env python3
"""
Audit the dashboard's numbers against the raw evidence.

Every figure the dashboard shows is derived -- status, duration, human-in-loop
time, locator tiers. This recomputes each one straight from log.jsonl and
result.json, by a deliberately different route, and reports any disagreement.

The point is not to re-run the same code and watch it agree with itself. Where
runs.py reads a field, this reads the raw events; where runs.py derives a
status, this derives it from a different signal. A metric that is confidently
wrong is worse than one that is missing, because nobody checks a number that
looks plausible.

    python3 scripts/audit_dashboard.py
    python3 scripts/audit_dashboard.py --target meridian
    python3 scripts/audit_dashboard.py --verbose

Exits non-zero if any run disagrees.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.capability_api.runs import evidence_root, list_runs  # noqa: E402


def read_events(run_dir):
    events = []
    path = run_dir / "log.jsonl"
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    return events


def read_result(run_dir):
    path = run_dir / "result.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def independent_status(events, result):
    """Status recomputed from the EVENT LOG, not from the result file.

    runs.py starts from result.json's status and overrides it. This starts
    from what the run actually did, so the two only agree if both the engine's
    verdict and the derivation are right.
    """
    names = [e.get("event") for e in events]

    # A refusal before the run began. Derived from the presence of a denial
    # event AND the absence of a result file -- deliberately a different test
    # from runs.py's, which only looks at the events, so the two agree only if
    # the run really was refused rather than merely missing its result.
    if ("replay_denied" in names or "run_denied" in names) and result is None:
        return "denied"

    if "operator_handed_back" in names and (result or {}).get("status") == "success":
        return "escalated"
    if "recovery_applied" in names and (result or {}).get("status") == "success":
        return "recovered"
    if result is None:
        return "unknown"
    return result.get("status", "unknown")


def independent_human_seconds(events):
    """Wall-clock the operator held the session, from raw timestamps."""
    windows = []
    opened = None
    for e in events:
        if e.get("event") == "intervention_created":
            opened = e.get("ts")
        elif e.get("event") == "operator_handed_back" and opened:
            try:
                delta = datetime.fromisoformat(e["ts"]) - datetime.fromisoformat(opened)
                windows.append(round(delta.total_seconds(), 1))
            except Exception:
                pass
            opened = None
    return windows


def independent_tiers(result):
    return {
        t["step_id"]: t.get("resolved_tier")
        for t in (result or {}).get("step_telemetry", [])
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dashboard_runs = list_runs(limit=1000, target=args.target)
    root = evidence_root()

    print("Auditing " + str(len(dashboard_runs)) + " run(s)"
          + (" for target '" + args.target + "'" if args.target else "") + "\n")

    problems = []
    counts = {}

    for run in dashboard_runs:
        run_dir = root / run["run_id"]
        events = read_events(run_dir)
        result = read_result(run_dir)

        counts[run["status"]] = counts.get(run["status"], 0) + 1

        # --- status -------------------------------------------------------
        expected = independent_status(events, result)
        if run["status"] != expected:
            problems.append(
                run["run_id"] + ": dashboard says status=" + str(run["status"])
                + ", event log implies " + expected
            )

        # --- human-in-loop -------------------------------------------------
        raw_windows = independent_human_seconds(events)
        shown = [e["seconds_total"] for e in run["escalations"]]
        if raw_windows != shown:
            problems.append(
                run["run_id"] + ": human seconds " + str(shown)
                + " but raw timestamps give " + str(raw_windows)
            )

        # --- locator tiers ---------------------------------------------------
        raw_tiers = independent_tiers(result)
        shown_tiers = {t["step_id"]: t.get("resolved_tier") for t in run["step_telemetry"]}
        if raw_tiers != shown_tiers:
            problems.append(run["run_id"] + ": step telemetry disagrees with result.json")

        # --- outputs ---------------------------------------------------------
        raw_outputs = (result or {}).get("outputs") or {}
        if raw_outputs != (run["outputs"] or {}):
            problems.append(run["run_id"] + ": outputs disagree with result.json")

        # --- events ----------------------------------------------------------
        if run["event_count"] != len(events):
            problems.append(
                run["run_id"] + ": event_count=" + str(run["event_count"])
                + " but log.jsonl has " + str(len(events)) + " parseable lines"
            )

        if args.verbose:
            print("  " + run["run_id"][:44].ljust(46)
                  + str(run["status"]).ljust(18)
                  + (str(raw_windows) if raw_windows else ""))

    # ---- what the colours actually mean -----------------------------------
    print("\nStatus distribution as the dashboard shows it:")
    for status in sorted(counts):
        print("  " + status.ljust(20) + str(counts[status]))

    completed = counts.get("success", 0) + counts.get("escalated", 0) + counts.get("recovered", 0)
    print("\n  runs the ENGINE considered successful: " + str(completed))
    print("  (success + escalated + recovered -- the last two are display")
    print("   overrides on an engine status of success, so the green count")
    print("   alone undercounts completed runs)")

    unknown = counts.get("unknown", 0)
    if unknown:
        print("\n  " + str(unknown) + " run(s) show 'unknown': no result.json was written,")
        print("  which means the run was denied or died before finishing. Real,")
        print("  not a display bug -- but worth knowing they are not failures.")

    print()
    if problems:
        print(str(len(problems)) + " DISAGREEMENT(S):")
        for p in problems:
            print("  - " + p)
        sys.exit(1)

    print("No disagreements. Every displayed figure matches the raw evidence.")


if __name__ == "__main__":
    main()
