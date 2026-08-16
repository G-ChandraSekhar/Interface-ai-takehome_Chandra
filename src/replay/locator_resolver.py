"""
Locator resolution: walk a step's ranked locator ladder against whatever
page replay is currently on, and report which tier actually resolved.

Deliberately duck-typed against `page` (works with a real Playwright Page
or a test double implementing .locator()/.get_by_role()/.get_by_text()) so
the ladder-walking logic itself is testable without a browser -- only the
Playwright-specific calls inside _resolve_one are real-browser-only.
"""

from __future__ import annotations


def _resolve_one(page, candidate):
    if candidate.strategy == "role_name":
        role, _, name = candidate.value.partition(":")
        return page.get_by_role(role, name=name, exact=True)
    if candidate.strategy in ("css_name_attr", "css_id"):
        return page.locator(candidate.value)
    if candidate.strategy == "text":
        return page.get_by_text(candidate.value, exact=True)
    # "positional" has no stable way to re-resolve -- it was recorded as a
    # last-resort marker, not an actionable locator. Treated as always
    # unresolvable; if it's the ONLY candidate, replay reports a clear
    # locator_not_found failure rather than guessing at a position.
    return None


def resolve_locator(page, candidates):
    """Returns (locator, strategy, tier_index) for the first candidate that
    resolves to exactly one element, or None if none of them do."""
    for i, candidate in enumerate(candidates):
        try:
            loc = _resolve_one(page, candidate)
        except Exception:
            continue
        if loc is None:
            continue
        try:
            count = loc.count()
        except Exception:
            continue
        if count == 1:
            return loc, candidate.strategy, i
    return None
