"""
Detectors: pure-text classification of what state the page is currently in.

Deliberately operate on plain page text (page.locator("body").inner_text()
in the real engine), not on a live Playwright Page -- this is what makes the
whole classification layer testable without a browser, and it mirrors
exactly what digest.py already captures during discovery, so the same
signal a human or the LLM would see is what replay checks against.

Every marker string here is specific to this target app's actual copy
(mock_app/templates/*.html is the source of truth) -- a real deployment
would source these per-vendor-app, which is exactly the kind of thing
REPORT.md's Cuts section should call out as "vendor-specific templates, not
yet a general library."
"""

from __future__ import annotations

from typing import Optional

from src.replay.result import FailureClass

# (marker substring, outcome_code, human message)
_BUSINESS_OUTCOME_MARKERS = [
    ("No record found for", "MEMBER_NOT_FOUND", "The requested member record does not exist."),
    (
        "is restricted. Permission denied.",
        "PERMISSION_DENIED",
        "Access to this record is restricted.",
    ),
]

_RECOVERABLE_MARKERS = [
    ("Your session has expired.", "session_timeout"),
]

_HARD_FAILURE_MARKERS = [
    ("System Error 500", FailureClass.APP_ERROR),
]


def detect_business_outcome(page_text: str):
    """Returns (outcome_code, message) or None."""
    for marker, code, message in _BUSINESS_OUTCOME_MARKERS:
        if marker in page_text:
            return code, message
    return None


def detect_recoverable(page_text: str) -> Optional[str]:
    """Returns a recovery condition name, or None."""
    for marker, condition in _RECOVERABLE_MARKERS:
        if marker in page_text:
            return condition
    return None


def detect_hard_failure(page_text: str) -> Optional[FailureClass]:
    for marker, failure_class in _HARD_FAILURE_MARKERS:
        if marker in page_text:
            return failure_class
    return None
