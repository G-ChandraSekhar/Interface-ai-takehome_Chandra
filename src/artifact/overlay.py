"""
Artifact overlay.

Per the brief's Section 3.7 (hundreds of tenants running the same vendor
product, configured/branded differently) and Section 8's stretch goal
("demonstrate one artifact recorded on a 'base' app being applied to a
second, slightly different variant... with per-variant overrides"): an
overlay is a small, reviewable JSON patch -- NOT a re-recording -- that
adapts a base artifact's target, checkpoint, and specific step locators to
a different tenant.

Design choice: the overlay patches only what actually differs between
tenants (here: origin/route prefix, the one form field whose underlying
HTML `name` attribute differs, and the checkpoint's route prefix). Every
step whose accessible name/role are identical across tenants (the "Search"
button, the "View record" link -- both rendered from the SAME shared
template, differing only in the tenant-specific strings the template
substitutes) needs NO override at all. This is the concrete mechanism for
"reuse across tenants running the same app" the brief asks for: most of an
artifact survives untouched; only the genuinely tenant-specific parts are
patched.
"""

from __future__ import annotations

import json

from src.artifact.schema import Artifact


def apply_overlay(base, overlay):
    """Returns a NEW Artifact with the overlay's fields merged in -- the
    base artifact object is never mutated."""
    data = json.loads(base.model_dump_json())

    if "target" in overlay:
        data["target"].update(overlay["target"])

    if "checkpoint" in overlay:
        data["checkpoint"].update(overlay["checkpoint"])

    for step_id, step_patch in overlay.get("step_overrides", {}).items():
        found = False
        for step in data["steps"]:
            if step["step_id"] == step_id:
                step.update(step_patch)
                found = True
                break
        if not found:
            raise ValueError("Overlay references unknown step_id: " + step_id)

    return Artifact.model_validate(data)


def load_overlay(path):
    with open(path, "r") as f:
        return json.load(f)


def apply_overlay_from_file(base, overlay_path):
    return apply_overlay(base, load_overlay(overlay_path))
