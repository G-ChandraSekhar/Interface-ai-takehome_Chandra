"""
Demo: an external "AI agent" discovering and invoking a capability purely
through the HTTP API -- no knowledge of Playwright, the mock app, or the
replay engine internals. This is the concrete proof of the brief's own
thesis: the artifact is a capability an agent can call, not just a script
a developer runs.

Usage (capability API server must already be running):
    uvicorn src.capability_api.server:app --port 8000
    python3 -m src.capability_api.demo_agent --member-id 8832
"""

from __future__ import annotations

import argparse
import json

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--member-id", default="8832")
    args = parser.parse_args()

    print("[agent] Discovering available capabilities...")
    resp = requests.get(args.base_url + "/capabilities")
    resp.raise_for_status()
    capabilities = resp.json()["capabilities"]

    print("[agent] Found " + str(len(capabilities)) + " capability(ies):")
    for cap in capabilities:
        fn = cap["function"]
        print("  - " + fn["name"] + "@" + str(cap["version"]) + ": " + fn["description"])

    target = None
    for c in capabilities:
        if c["function"]["name"] == "lookup_member_savings_balance":
            target = c
            break
    if target is None:
        print("[agent] Capability not found -- nothing to invoke.")
        return

    print("[agent] Selecting 'lookup_member_savings_balance' and invoking with member_id=" + args.member_id)
    invoke_resp = requests.post(
        args.base_url + "/capabilities/lookup_member_savings_balance/invoke",
        json={"params": {"member_id": args.member_id}, "headless": True},
    )
    invoke_resp.raise_for_status()
    result = invoke_resp.json()

    print("[agent] Result:")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
