"""
Command-line entrypoint.

    python3 -m src.cli discover --goal "..." --tenant a --param member_id=4521 \\
        --output member_name --output savings_balance

Replay subcommand arrives in Phase 4.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.discovery.loop import run_discovery

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
    )

    print(f"\nStatus: {result.status}")
    print(f"Message: {result.message}")
    print(f"Steps: {result.step_count}")
    print(f"Outputs: {result.outputs}")
    print(f"Evidence written to: {result.run_dir}")

    sys.exit(0 if result.status == "success" else 1)


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
    p_discover.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
