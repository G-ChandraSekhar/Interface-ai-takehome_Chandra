#!/usr/bin/env python3
"""
Re-sync artifacts' declared detector patterns from their target's config.

Why this exists
---------------
`distill.py` snapshots the target's detector taxonomy onto each artifact at
record time, and replay prefers an artifact's own patterns over the target
config. That is the intended design: a reviewer approving an artifact is also
approving what counts as "not found" or "rejected" for it, and an artifact
keeps classifying the way its reviewer saw it even if the config later moves.

The cost of that design is this script. When a marker turns out to be *wrong*
rather than merely different -- as "The transaction could not be validated:"
was, matching the funds-transfer screen but not the open-share screen that says
"The request could not be validated:" -- every artifact recorded before the fix
still carries the broken copy. Re-recording six capabilities to correct a
string would be absurd; hand-editing six JSON files is worse.

    python3 scripts/resync_detectors.py --dry-run
    python3 scripts/resync_detectors.py

Deliberately explicit rather than automatic. Replay never silently refreshes an
artifact's detectors, because that would mean the classification a reviewer
signed off on could change under them without anyone deciding to. This is
someone deciding to, and the diff it produces is reviewable in the same way the
artifact itself is.

Nothing else about the artifact is touched -- not the steps, not the policy,
not `approved`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.replay.detectors import detectors_from_target  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts-dir", default=str(REPO_ROOT / "artifacts"))
    ap.add_argument("--target", default=None, help="only artifacts for this target")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    changed = 0

    for path in sorted(artifacts_dir.glob("*.json")):
        data = json.loads(path.read_text())
        tenant = (data.get("target") or {}).get("tenant")
        if not tenant or (args.target and tenant != args.target):
            continue

        fresh = detectors_from_target(tenant)
        if fresh is None:
            print("  skip     " + path.name + "  (target '" + str(tenant) + "' declares none)")
            continue

        new_block = json.loads(fresh.model_dump_json())
        if data.get("detectors") == new_block:
            print("  current  " + path.name)
            continue

        old_markers = {
            m["marker"]
            for group in (data.get("detectors") or {}).values()
            for m in group
        }
        new_markers = {m["marker"] for group in new_block.values() for m in group}

        print("  UPDATE   " + path.name)
        for marker in sorted(old_markers - new_markers):
            print("      - " + repr(marker))
        for marker in sorted(new_markers - old_markers):
            print("      + " + repr(marker))

        if not args.dry_run:
            data["detectors"] = new_block
            path.write_text(json.dumps(data, indent=2) + "\n")
        changed += 1

    print()
    if args.dry_run:
        print(str(changed) + " artifact(s) would change. Re-run without --dry-run to apply.")
    else:
        print(str(changed) + " artifact(s) updated.")


if __name__ == "__main__":
    main()
