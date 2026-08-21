"""
Command-line entrypoint.

    python3 -m src.cli discover --goal "..." --tenant a --param member_id=4521 \\
        --output member_name --output savings_balance

    python3 -m src.cli distill --run-dir evidence/discovery_<run_id> \\
        --artifact-id lookup_member_savings_balance \\
        --name "Look up member savings balance" \\
        --param member_id=4521 --output member_name --output savings_balance

    python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \\
        --param member_id=8832

    python3 -m src.cli health
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.artifact.distill import DistillationError, distill_run
from src.artifact.overlay import apply_overlay_from_file
from src.artifact.store import load_artifact_by_id, save_artifact
from src.discovery.loop import run_discovery
from src.replay.engine import replay_artifact
from src.replay.result import ReplayStatus
from src.replay.stability import run_stability
from src.telemetry.health import STATUS_DEGRADING, STATUS_INSUFFICIENT, assess_health
from src.telemetry.record import (
    DEFAULT_TELEMETRY_PATH,
    SOURCE_REPLAY,
    SOURCE_STABILITY,
    load_records,
)

# tenant/target id -> (base_url, route_prefix). The tenant letters are the
# original mock app; "meridian" is the adaptation target. Both resolve to a
# config/targets/*.yaml through src/targets.py -- see load_target().
TENANT_BASE_URLS = {
    "a": ("http://localhost:4478", "/desk"),
    "b": ("http://localhost:4479", "/operations"),
    "meridian": ("https://web-sample.interface-hiring.com", ""),
}


def _parse_kv(items: list[str]) -> dict:
    result = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected key=value, got: {item}")
        k, v = item.split("=", 1)
        result[k] = v
    return result


def cmd_discover(args):
    base_url, route_prefix = TENANT_BASE_URLS[args.tenant]
    params = _parse_kv(args.param or [])

    result = run_discovery(
        goal=args.goal,
        base_url=base_url,
        route_prefix=route_prefix,
        params=params,
        required_outputs=args.output or [],
        target_id=args.tenant,
        mutate_confirmed=args.mutate,
        irreversible_confirmed=args.confirm_irreversible,
        mock_auth=not args.no_mock_auth,
        headless=args.headless,
        model=args.model,
        handoff=args.handoff,
        console_port=args.console_port,
    )

    print(f"\nStatus: {result.status}")
    print(f"Message: {result.message}")
    print(f"Steps: {result.step_count}")
    print(f"Outputs: {result.outputs}")
    print(f"Evidence written to: {result.run_dir}")

    sys.exit(0 if result.status == "success" else 1)


def cmd_distill(args):
    run_dir = Path(args.run_dir)
    log_path = run_dir / "log.jsonl"
    if not log_path.exists():
        print(f"No log.jsonl found at {log_path}")
        sys.exit(1)

    params = _parse_kv(args.param or [])

    try:
        artifact = distill_run(
            log_path,
            artifact_id=args.artifact_id,
            name=args.name,
            params=params,
            required_outputs=args.output or [],
            version=args.version,
            optional_params=args.optional,
            description=args.description,
            enums={
                k: v.split(",")
                for k, v in (e.split("=", 1) for e in (args.enum or []))
            },
        )
    except DistillationError as e:
        print(f"Distillation failed: {e}")
        sys.exit(1)

    artifacts_dir = Path(args.artifacts_dir)
    saved_path = save_artifact(artifact, artifacts_dir)

    print(f"\nDistilled artifact: {saved_path}")
    print(f"  target: {artifact.target.tenant} ({artifact.target.base_url}{artifact.target.route_prefix})")
    print(f"  input_params: {list(artifact.input_params.keys())}")
    print(f"  output_schema: {list(artifact.output_schema.keys())}")
    print(f"  steps: {len(artifact.steps)}")
    print(f"  checkpoint: {artifact.checkpoint.url_pattern}")


def cmd_replay(args):
    artifacts_dir = Path(args.artifacts_dir)
    try:
        artifact = load_artifact_by_id(args.artifact_id, args.version, artifacts_dir)
    except FileNotFoundError:
        print(f"No artifact found: {args.artifact_id}@{args.version} in {artifacts_dir}")
        sys.exit(1)

    if args.overlay:
        overlay_path = Path(args.overlay)
        if not overlay_path.exists():
            print(f"Overlay file not found: {overlay_path}")
            sys.exit(1)
        artifact = apply_overlay_from_file(artifact, overlay_path)
        print(f"Applied overlay: {overlay_path} -> target={artifact.target.tenant} ({artifact.target.base_url}{artifact.target.route_prefix})")

    params = _parse_kv(args.param or [])

    result = replay_artifact(
        artifact,
        params,
        mutate_confirmed=args.mutate,
        mock_auth=not args.no_mock_auth,
        headless=args.headless,
        chaos=args.chaos,
        error_rate=args.error_rate,
        handoff=args.handoff,
        console_port=args.console_port,
    )

    print(f"\nStatus: {result.status.value}")
    if result.status == ReplayStatus.SUCCESS:
        print(f"Outputs: {result.outputs}")
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        print(f"Outcome code: {result.outcome_code}")
        print(f"Message: {result.outcome_message}")
    else:
        print(f"Failure class: {result.failure.step_class.value}")
        print(f"Step: {result.failure.step_id}")
        print(f"Expected: {result.failure.expected}")
        print(f"Observed: {result.failure.observed}")
    print(f"Step telemetry: {[(t.step_id, t.resolved_strategy, t.recovery_applied) for t in result.step_telemetry]}")
    print(f"Evidence written to: {result.run_dir}")

    sys.exit(0 if result.status == ReplayStatus.SUCCESS else 1)


def cmd_stability(args):
    artifacts_dir = Path(args.artifacts_dir)
    try:
        artifact = load_artifact_by_id(args.artifact_id, args.version, artifacts_dir)
    except FileNotFoundError:
        print(f"No artifact found: {args.artifact_id}@{args.version} in {artifacts_dir}")
        sys.exit(1)

    params = _parse_kv(args.param or [])

    report = run_stability(
        artifact,
        params,
        args.runs,
        mutate_confirmed=args.mutate,
        mock_auth=not args.no_mock_auth,
        headless=args.headless,
    )

    print(f"\nStability report for {args.artifact_id}@{args.version} ({args.runs} runs)")
    print(f"  success_rate:          {report.success_rate:.0%} ({report.successes}/{report.runs})")
    print(f"  business_outcome_rate: {report.business_outcome_rate:.0%} ({report.business_outcomes}/{report.runs})")
    print(f"  failure_rate:          {report.failure_rate:.0%} ({report.failures}/{report.runs})")
    if report.failure_classes:
        print(f"  failure classes seen:  {report.failure_classes}")
    print(f"  per-step avg locator tier:   {report.step_avg_tier}")
    print(f"  per-step worst locator tier: {report.step_worst_tier}")

    if args.update_artifact:
        updated = artifact.model_copy(update={"stability": report.to_artifact_stability()})
        saved_path = save_artifact(updated, artifacts_dir)
        print(f"\nWrote stability report to {saved_path} (artifact.approved left untouched -- see")
        print("ArtifactStability's docstring: this is a signal for a human reviewer, not a decision.)")

    # Not a pass/fail exit code on purpose -- an operator reviewing
    # reliability isn't asking a yes/no question the way a single replay
    # is; the report itself is the deliverable.
    sys.exit(0)


def cmd_health(args):
    """Reports which artifacts are drifting, across replay history.

    `stability` asks "is this artifact reliable right now" by replaying it N
    times back to back. That is a snapshot, and a snapshot cannot see a step
    that used to resolve at tier 1 and now resolves at tier 3. This command is
    the same telemetry read across time instead of across one sitting.
    """
    telemetry_path = Path(args.telemetry_path)

    sources = (SOURCE_REPLAY, SOURCE_STABILITY) if args.include_stability else (SOURCE_REPLAY,)
    records, skipped = load_records(
        telemetry_path, artifact_id=args.artifact_id, sources=sources
    )

    if not records:
        print(f"No replay history at {telemetry_path}.")
        print("Run some replays first -- history accumulates one line per run.")
        sys.exit(0)

    report = assess_health(
        records,
        baseline_n=args.baseline,
        recent_n=args.recent,
        min_runs=args.min_runs,
        records_skipped=skipped,
    )

    if args.json:
        print(report.model_dump_json(indent=2))
        sys.exit(1 if report.has_degradation else 0)

    print(f"\nArtifact health from {report.records_read} runs ({telemetry_path})")
    if report.records_skipped:
        # Surfaced rather than swallowed: a history file that is quietly losing
        # lines is quietly losing the signal this whole command exists to find.
        print(f"  note: {report.records_skipped} unreadable line(s) skipped")

    for artifact in report.artifacts:
        marker = "DEGRADING" if artifact.status == STATUS_DEGRADING else artifact.status.upper()
        print(f"\n  {artifact.artifact_id}  [{marker}]")

        if artifact.status == STATUS_INSUFFICIENT:
            for note in artifact.notes:
                print(f"    {note}")
            continue

        print(
            f"    windows: {artifact.baseline_runs} baseline vs "
            f"{artifact.recent_runs} recent (of {artifact.runs_seen} runs)"
        )
        if artifact.baseline_failure_rate is not None:
            print(
                f"    failure rate: {artifact.baseline_failure_rate:.0%} -> "
                f"{artifact.recent_failure_rate:.0%}"
            )

        for step in artifact.steps:
            if step.status == STATUS_DEGRADING:
                print(
                    f"    {step.step_id}: tier {step.baseline_tier:.1f} -> "
                    f"{step.recent_tier:.1f}  "
                    f"({step.baseline_strategy} -> {step.recent_strategy})  DRIFTING"
                )
            elif step.recent_tier is not None:
                print(f"    {step.step_id}: tier {step.recent_tier:.1f}  {step.status}")

        for note in artifact.notes:
            print(f"    note: {note}")

    if report.has_degradation:
        names = ", ".join(a.artifact_id for a in report.degrading)
        print(f"\nDegradation detected in: {names}")
        print("These artifacts still pass. They are resolving further down their")
        print("locator ladders than they used to, which is what precedes failing.")

    # Unlike `stability`, this DOES exit non-zero on a bad signal. The two
    # commands answer different questions: stability is a report a human reads
    # once before approving a capability, health is a check something runs on a
    # schedule and needs to act on without parsing text.
    sys.exit(1 if report.has_degradation else 0)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run a live LLM-driven discovery capture")
    p_discover.add_argument("--goal", required=True)
    p_discover.add_argument(
        "--tenant",
        choices=sorted(TENANT_BASE_URLS),
        default="a",
        help="which target console to discover against",
    )
    p_discover.add_argument("--param", action="append", help="key=value, repeatable")
    p_discover.add_argument("--output", action="append", help="required output name, repeatable")
    p_discover.add_argument("--mutate", action="store_true", help="allow mutating-tier actions")
    p_discover.add_argument(
        "--confirm-irreversible", action="store_true", help="allow the irreversible confirm step"
    )
    p_discover.add_argument("--no-mock-auth", action="store_true")
    p_discover.add_argument("--headless", action="store_true")
    p_discover.add_argument("--model", default=None)
    p_discover.add_argument(
        "--handoff", action="store_true", help="escalate to a human operator console if the agent gets stuck"
    )
    p_discover.add_argument("--console-port", type=int, default=4590)
    p_discover.set_defaults(func=cmd_discover)

    p_distill = sub.add_parser("distill", help="Distill a discovery run's log into a capability artifact")
    p_distill.add_argument("--run-dir", required=True, help="e.g. evidence/discovery_<run_id>")
    p_distill.add_argument("--artifact-id", required=True)
    p_distill.add_argument("--name", required=True)
    p_distill.add_argument("--version", type=int, default=1)
    p_distill.add_argument("--param", action="append", help="key=value used in the run, repeatable")
    p_distill.add_argument(
        "--description",
        help="what the CATALOG publishes for this capability. Defaults to "
             "--name. Never the discovery goal: that names the member the "
             "recording used and carries instructions aimed at the model "
             "doing the recording, not at anyone calling the result.",
    )
    p_distill.add_argument(
        "--enum", action="append", metavar="NAME=a,b,c",
        help="the exact values a dropdown parameter accepts. Published in the "
             "tool schema, where a model API enforces it -- an example alone "
             "is a hint the model may paraphrase. Repeatable.",
    )
    p_distill.add_argument(
        "--optional", action="append", metavar="NAME",
        help="an input param the caller may omit at replay. Its step is "
             "skipped rather than filled with an empty value, so the field is "
             "left as it is. Repeatable.",
    )
    p_distill.add_argument("--output", action="append", help="required output name, repeatable")
    p_distill.add_argument("--artifacts-dir", default="artifacts")
    p_distill.set_defaults(func=cmd_distill)

    p_replay = sub.add_parser("replay", help="Deterministically replay a saved artifact -- no LLM")
    p_replay.add_argument("--artifact-id", required=True)
    p_replay.add_argument("--version", type=int, default=1)
    p_replay.add_argument("--param", action="append", help="key=value, repeatable")
    p_replay.add_argument("--mutate", action="store_true", help="allow mutating-tier actions")
    p_replay.add_argument(
        "--handoff",
        action="store_true",
        help=(
            "route irreversible-tier steps to a human operator console. Irreversible "
            "steps can never run unattended, so without this flag a replay that "
            "reaches one fails closed."
        ),
    )
    p_replay.add_argument("--console-port", type=int, default=4590)
    p_replay.add_argument("--no-mock-auth", action="store_true")
    p_replay.add_argument("--headless", action="store_true")
    p_replay.add_argument(
        "--chaos",
        default="none",
        choices=[
            "none",
            # the original mock target's modes
            "session_timeout", "error500", "supervisor", "slow",
            # MERIDIAN's, driven through its own System Settings screen
            "validation", "notfound", "permission", "timeout", "maintenance", "server",
        ],
        help=(
            "force a fault for this replay run. On MERIDIAN this is set on the "
            "host's own settings screen and fires on EVERY request, so recovery "
            "retries within its budget and then fails cleanly -- use --error-rate "
            "for a transient fault that recovery can actually clear."
        ),
    )
    p_replay.add_argument(
        "--error-rate",
        type=float,
        default=0.0,
        help=(
            "0.0-1.0 chance of a random fault per posting action, set on the "
            "target's own controls. A transient fault, so this is what "
            "demonstrates recovery succeeding rather than exhausting."
        ),
    )
    p_replay.add_argument("--artifacts-dir", default="artifacts")
    p_replay.add_argument(
        "--overlay", default=None, help="path to a tenant overlay JSON to patch the base artifact with"
    )
    p_replay.set_defaults(func=cmd_replay)

    p_stability = sub.add_parser(
        "stability",
        help="Replay a saved artifact N times and report a reliability/drift signal",
    )
    p_stability.add_argument("--artifact-id", required=True)
    p_stability.add_argument("--version", type=int, default=1)
    p_stability.add_argument("--param", action="append", help="key=value, repeatable")
    p_stability.add_argument("--runs", type=int, default=5)
    p_stability.add_argument("--mutate", action="store_true", help="allow mutating-tier actions")
    p_stability.add_argument("--no-mock-auth", action="store_true")
    p_stability.add_argument("--headless", action="store_true")
    p_stability.add_argument("--artifacts-dir", default="artifacts")
    p_stability.add_argument(
        "--update-artifact",
        action="store_true",
        help="write the computed report back onto the saved artifact's 'stability' field",
    )
    p_stability.set_defaults(func=cmd_stability)

    p_health = sub.add_parser(
        "health",
        help="Report artifacts drifting down their locator ladders, across replay history",
    )
    p_health.add_argument(
        "--artifact-id", default=None, help="restrict to one artifact (default: all)"
    )
    p_health.add_argument("--telemetry-path", default=str(DEFAULT_TELEMETRY_PATH))
    p_health.add_argument(
        "--baseline", type=int, default=10, help="runs forming the baseline window"
    )
    p_health.add_argument(
        "--recent", type=int, default=5, help="most recent runs to compare against it"
    )
    p_health.add_argument(
        "--min-runs", type=int, default=6, help="below this, report insufficient_data"
    )
    p_health.add_argument(
        "--include-stability",
        action="store_true",
        help=(
            "include runs produced by the stability command. Excluded by default: "
            "N runs fired back to back in one minute would swamp a baseline meant "
            "to span weeks."
        ),
    )
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=cmd_health)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
