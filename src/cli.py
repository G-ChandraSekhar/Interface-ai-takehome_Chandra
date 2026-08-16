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

TENANT_BASE_URLS = {
    "a": ("http://localhost:4478", "/desk"),
    "b": ("http://localhost:4479", "/operations"),
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
        irreversible_confirmed=args.confirm_irreversible,
        mock_auth=not args.no_mock_auth,
        headless=args.headless,
        chaos=args.chaos,
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


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run a live LLM-driven discovery capture")
    p_discover.add_argument("--goal", required=True)
    p_discover.add_argument("--tenant", choices=["a", "b"], default="a")
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
    p_distill.add_argument("--output", action="append", help="required output name, repeatable")
    p_distill.add_argument("--artifacts-dir", default="artifacts")
    p_distill.set_defaults(func=cmd_distill)

    p_replay = sub.add_parser("replay", help="Deterministically replay a saved artifact -- no LLM")
    p_replay.add_argument("--artifact-id", required=True)
    p_replay.add_argument("--version", type=int, default=1)
    p_replay.add_argument("--param", action="append", help="key=value, repeatable")
    p_replay.add_argument("--mutate", action="store_true", help="allow mutating-tier actions")
    p_replay.add_argument(
        "--confirm-irreversible", action="store_true", help="allow the irreversible confirm step"
    )
    p_replay.add_argument("--no-mock-auth", action="store_true")
    p_replay.add_argument("--headless", action="store_true")
    p_replay.add_argument(
        "--chaos",
        default="none",
        choices=["none", "session_timeout", "error500", "supervisor", "slow"],
        help="deterministic fault to inject for this replay run",
    )
    p_replay.add_argument("--artifacts-dir", default="artifacts")
    p_replay.add_argument(
        "--overlay", default=None, help="path to a tenant overlay JSON to patch the base artifact with"
    )
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
