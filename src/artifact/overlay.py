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


class OverlayMismatchError(ValueError):
    pass


def apply_overlay(base, overlay):
    """Returns a NEW Artifact with the overlay's fields merged in -- the
    base artifact object is never mutated.

    If the overlay declares "capability_id", it must match the base
    artifact's artifact_id, or this raises. Without this check, applying
    the wrong tenant's overlay to the wrong base artifact would silently
    produce a structurally valid but semantically nonsensical artifact --
    e.g. patching a sub-account-opening artifact's steps with a lookup
    artifact's overlay. The field is optional (older or hand-written
    overlays may not have it) so this only guards when the information is
    actually present, but every overlay this project writes going forward
    includes it.
    """
    if "capability_id" in overlay and overlay["capability_id"] != base.artifact_id:
        raise OverlayMismatchError(
            "Overlay targets capability '"
            + overlay["capability_id"]
            + "' but base artifact is '"
            + base.artifact_id
            + "' -- refusing to apply a mismatched overlay."
        )

    data = json.loads(base.model_dump_json())

    if "target" in overlay:
        data["target"].update(overlay["target"])

        # An overlay that retargets the origin must also carry that origin
        # into the artifact's own policy, or the artifact would refuse to
        # run against the very tenant the overlay exists to reach. This is
        # not a weakening of the check: approving a tenant overlay IS the
        # reviewer approving that tenant's origin for this capability, and
        # the resulting artifact is still strictly bounded -- it permits the
        # overlay's origin and the base's, nothing wider. The global
        # operator policy in config/allowlist.yaml still applies on top and
        # is unaffected by anything an overlay says.
        new_base_url = overlay["target"].get("base_url")
        if new_base_url and data.get("policy"):
            allowed = data["policy"]["allowed_origins"]
            if new_base_url not in allowed:
                data["policy"]["allowed_origins"] = allowed + [new_base_url]

    if "checkpoint" in overlay:
        data["checkpoint"].update(overlay["checkpoint"])

    # An overlay may also explicitly widen/narrow the artifact policy, for
    # cases the origin rule above doesn't cover (e.g. a tenant whose flow
    # genuinely needs an extra action kind). Explicit and reviewable.
    if "policy" in overlay and data.get("policy"):
        data["policy"].update(overlay["policy"])

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
