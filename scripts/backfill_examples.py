#!/usr/bin/env python3
"""
Print the distill command that would re-create each artifact WITH examples.

`ParamSpec.example` is filled at distill time, so every artifact recorded
before that field existed has none -- and a model reading
`"share_id": {"type": "string"}` has nothing to go on. It sent `S0070` for a
share whose options read `100234-S0070`, which is a perfectly reasonable
reading of a contract that says nothing about format.

Artifacts do not record which discovery run produced them, so this matches on
what IS recoverable: the same target, the same set of input parameter names,
and the same required outputs. Where more than one run matches it prints all
of them and leaves the choice to a person -- picking silently would be exactly
the kind of confident guess this whole exercise keeps punishing.

    python3 scripts/backfill_examples.py             # latest version of each
    python3 scripts/backfill_examples.py --all       # every version

Prints commands. Runs nothing.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def discovery_runs():
    """Every successful discovery run, with the params and outputs it used."""
    runs = []
    for d in sorted((REPO / "evidence").glob("discovery_*")):
        result, params, goal = d / "result.json", None, None
        if not result.exists():
            continue
        try:
            data = json.loads(result.read_text())
        except Exception:
            continue
        if data.get("status") != "success":
            continue

        for line in (d / "log.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("event") == "run_started":
                params = event.get("params") or {}
                goal = event.get("goal")
                break

        if params:
            runs.append({
                "dir": d.name,
                "params": params,
                "outputs": sorted((data.get("outputs") or {})),
                "goal": goal,
            })
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every version, not just the latest")
    args = ap.parse_args()

    runs = discovery_runs()
    artifacts = {}

    for path in sorted((REPO / "artifacts").glob("*.json")):
        data = json.loads(path.read_text())
        if (data.get("target") or {}).get("tenant") != "meridian":
            continue
        aid, version = data["artifact_id"], data["version"]
        if not args.all:
            if aid in artifacts and artifacts[aid][0] >= version:
                continue
        artifacts[aid + ("@" + str(version) if args.all else "")] = (version, data)

    for key in sorted(artifacts):
        version, data = artifacts[key]
        aid = data["artifact_id"]
        needs = [n for n, spec in data["input_params"].items() if not spec.get("example")]
        if not needs:
            print("# " + aid + "@" + str(version) + " already has examples\n")
            continue

        want_params = set(data["input_params"])
        want_outputs = sorted(data["output_schema"])
        matches = [
            r for r in runs
            if set(r["params"]) == want_params and r["outputs"] == want_outputs
        ]

        print("# " + "-" * 68)
        print("# " + aid + "@" + str(version) + " -- missing: " + ", ".join(needs))

        if not matches:
            print("# NO discovery run matches these params and outputs.")
            print("# Re-record it, or leave it: an artifact without examples still")
            print("# replays correctly -- it just tells a caller less.\n")
            continue

        if len(matches) > 1:
            print("# " + str(len(matches)) + " runs match. Check the goals and pick one:")
            for m in matches:
                print("#   " + m["dir"] + "  " + str(m["params"]))
            print("#")

        run = matches[-1]
        optional = [n for n, spec in data["input_params"].items() if not spec.get("required", True)]

        cmd = ["python3", "-m", "src.cli", "distill",
               "--run-dir", "evidence/" + run["dir"],
               "--artifact-id", aid, "--version", str(version),
               "--name", data.get("name") or aid]
        if data.get("description"):
            cmd += ["--description", data["description"]]
        for name, value in run["params"].items():
            cmd += ["--param", name + "=" + str(value)]
        for name in optional:
            cmd += ["--optional", name]
        for name in want_outputs:
            cmd += ["--output", name]

        print(" \\\n  ".join(
            " ".join(shlex.quote(c) for c in cmd[i:i + 4])
            for i in range(0, len(cmd), 4)
        ))
        print()

    print("# " + "-" * 68)
    print("# Re-distilling overwrites the artifact in place. Nothing else about")
    print("# it changes -- same steps, same checkpoint, same detectors -- so a")
    print("# capability that replayed before still replays after.")


if __name__ == "__main__":
    main()
