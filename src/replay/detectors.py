"""
Detectors: pure-text classification of what state the page is currently in.

Deliberately operate on plain page text (page.locator("body").inner_text()
in the real engine), not on a live Playwright Page -- this is what makes the
whole classification layer testable without a browser, and it mirrors
exactly what digest.py already captures during discovery, so the same
signal a human or the LLM would see is what replay checks against.

The marker lists below are this target app's actual copy
(mock_app/templates/*.html is the source of truth), kept here as the
DEFAULT patterns for artifacts that don't declare their own. An artifact
distilled from a different vendor app declares its own patterns on
Artifact.detectors (src/artifact/schema.py's ArtifactDetectors) -- a
reviewer approving that artifact is thereby also approving what counts as
"not found" or "session expired" for it, without anyone editing this file.
"""

from __future__ import annotations

from typing import Optional

from src.replay.result import FailureClass

# (marker substring, outcome_code, human message)
_DEFAULT_BUSINESS_OUTCOME_MARKERS = [
    ("No record found for", "MEMBER_NOT_FOUND", "The requested member record does not exist."),
    (
        "is restricted. Permission denied.",
        "PERMISSION_DENIED",
        "Access to this record is restricted.",
    ),
]

_DEFAULT_RECOVERABLE_MARKERS = [
    ("Your session has expired.", "session_timeout"),
]

_DEFAULT_HARD_FAILURE_MARKERS = [
    ("System Error 500", FailureClass.APP_ERROR),
]


def detect_business_outcome(page_text: str, patterns=None):
    """Returns (outcome_code, message) or None.

    patterns: optional list of ArtifactDetectors.business_outcomes
    (DetectorPattern objects) from the artifact being replayed. Falls back
    to this target app's own defaults when the artifact declares none
    (older artifacts, or artifacts distilled before this field existed).
    """
    if patterns:
        for p in patterns:
            if p.marker in page_text:
                return p.code, p.message
        return None
    for marker, code, message in _DEFAULT_BUSINESS_OUTCOME_MARKERS:
        if marker in page_text:
            return code, message
    return None


def detect_recoverable(page_text: str, patterns=None) -> Optional[str]:
    """Returns a recovery condition name, or None."""
    if patterns:
        for p in patterns:
            if p.marker in page_text:
                return p.code
        return None
    for marker, condition in _DEFAULT_RECOVERABLE_MARKERS:
        if marker in page_text:
            return condition
    return None


def detect_hard_failure(page_text: str, patterns=None) -> Optional[FailureClass]:
    """Note: hard-failure codes stay as FailureClass enum members even for
    artifact-declared patterns (DetectorPattern.code is a plain string) --
    converted here via FailureClass(p.code) so the artifact's JSON stays
    plain text/serializable while replay still gets a real enum value."""
    if patterns:
        for p in patterns:
            if p.marker in page_text:
                return FailureClass(p.code)
        return None
    for marker, failure_class in _DEFAULT_HARD_FAILURE_MARKERS:
        if marker in page_text:
            return failure_class
    return None

# ---------------------------------------------------------------------------
# Target-configured defaults
# ---------------------------------------------------------------------------
#
# The marker lists above are the ORIGINAL target's copy, kept as a last-resort
# fallback so artifacts distilled before targets existed still classify. Every
# target now declares its own taxonomy in config/targets/<id>.yaml, which is
# what makes the three-way contract portable: a new vendor console is a YAML
# file, not an edit to this module.

def detectors_from_target(target_id):
    """The ArtifactDetectors declared by a target's config, or None.

    Returns None for an unknown target or one that declares no detectors, in
    which case callers fall back to the module constants above.
    """
    from src.artifact.schema import ArtifactDetectors
    from src.targets import load_target

    target = load_target(target_id)
    if target is None or not target.detectors:
        return None
    try:
        return ArtifactDetectors(**target.detectors)
    except Exception:
        return None


def detect_recoverable_pattern(page_text, patterns=None):
    """The matching recoverable DetectorPattern, or None.

    detect_recoverable() above returns only the condition name, which was
    enough when there was one hardcoded way to recover. It is kept for
    callers that only need the name; this returns the whole pattern so the
    engine can read its declared RecoveryAction.
    """
    from src.artifact.schema import DetectorPattern

    if patterns:
        for p in patterns:
            if p.marker in page_text:
                return p
        return None
    for marker, condition in _DEFAULT_RECOVERABLE_MARKERS:
        if marker in page_text:
            # Synthesised so the engine has one shape to handle. No declared
            # recovery means the legacy behaviour: click through, stay put.
            return DetectorPattern(marker=marker, code=condition)
    return None
