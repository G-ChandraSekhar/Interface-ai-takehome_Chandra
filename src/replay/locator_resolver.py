"""
Locator resolution: walk a step's ranked locator ladder against whatever
page replay is currently on, report which tier resolved, and -- when
nothing resolves -- report exactly WHY each candidate was rejected.

The per-candidate diagnostics matter for debuggability. "No candidate in
the ladder resolved" tells an operator almost nothing; "role_name matched 3
elements, css_name_attr matched 0, text matched 1 but it was not visible"
tells them precisely what changed about the page. This is the difference
between a failure bundle you can act on and one you have to reproduce
manually to understand.

Rejection reasons:
  not_applicable  -- strategy has no re-resolvable form (a "positional"
                     marker recorded when no stable locator existed)
  error           -- the locator call itself threw (malformed selector,
                     detached frame, etc.)
  no_match        -- resolved to zero elements
  not_unique      -- resolved to more than one element (ambiguous, so
                     acting on it would be a guess)
  not_visible     -- resolved uniquely, but the element is hidden
  disabled        -- resolved uniquely and visible, but not interactable

Deliberately duck-typed against `page` (works with a real Playwright Page
or a test double) so the ladder-walking and diagnostic logic is testable
without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ResolutionAttempt:
    """One candidate's outcome. `matched` is the element count when it could
    be determined, else None."""

    tier: int
    strategy: str
    value: str
    ok: bool
    reason: Optional[str] = None
    matched: Optional[int] = None

    def to_dict(self):
        return {
            "tier": self.tier,
            "strategy": self.strategy,
            "value": self.value,
            "ok": self.ok,
            "reason": self.reason,
            "matched": self.matched,
        }


@dataclass
class Resolution:
    locator: object
    strategy: str
    tier: int
    attempts: list

    def attempts_as_dicts(self):
        return [a.to_dict() for a in self.attempts]


@dataclass
class ResolutionFailure:
    attempts: list

    def attempts_as_dicts(self):
        return [a.to_dict() for a in self.attempts]

    def summary(self):
        """A one-line, human-readable account of why the ladder failed --
        goes straight into the failure result's `observed` field."""
        if not self.attempts:
            return "No locator candidates were recorded for this step."
        parts = []
        for a in self.attempts:
            detail = a.reason or "unknown"
            if a.matched is not None:
                detail = detail + " (matched " + str(a.matched) + ")"
            parts.append("tier " + str(a.tier) + " " + a.strategy + ": " + detail)
        return "; ".join(parts)


def _resolve_one(page, candidate):
    if candidate.strategy == "role_name":
        role, _, name = candidate.value.partition(":")
        return page.get_by_role(role, name=name, exact=True)
    if candidate.strategy in ("css_name_attr", "css_id"):
        return page.locator(candidate.value)
    if candidate.strategy == "text":
        return page.get_by_text(candidate.value, exact=True)
    # "positional" has no stable way to re-resolve -- it was recorded as a
    # last-resort marker, not an actionable locator.
    return None


def _is_visible(loc):
    """Visibility/enabled checks are best-effort: a test double may not
    implement them, in which case we don't treat their absence as a
    rejection -- only an explicit False counts."""
    try:
        return loc.is_visible()
    except (AttributeError, NotImplementedError):
        return True
    except Exception:
        return True


def _is_enabled(loc):
    try:
        return loc.is_enabled()
    except (AttributeError, NotImplementedError):
        return True
    except Exception:
        return True


def resolve_locator(page, candidates):
    """Returns a Resolution for the first candidate that resolves to exactly
    one visible, enabled element, or a ResolutionFailure carrying every
    candidate's rejection reason."""
    attempts = []

    for i, candidate in enumerate(candidates):
        tier = i + 1

        try:
            loc = _resolve_one(page, candidate)
        except Exception as e:
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "error: " + str(e)[:120])
            )
            continue

        if loc is None:
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "not_applicable")
            )
            continue

        try:
            count = loc.count()
        except Exception as e:
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "error: " + str(e)[:120])
            )
            continue

        if count == 0:
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "no_match", matched=0)
            )
            continue

        if count > 1:
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "not_unique", matched=count)
            )
            continue

        if not _is_visible(loc):
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "not_visible", matched=1)
            )
            continue

        if not _is_enabled(loc):
            attempts.append(
                ResolutionAttempt(tier, candidate.strategy, candidate.value, False, "disabled", matched=1)
            )
            continue

        attempts.append(
            ResolutionAttempt(tier, candidate.strategy, candidate.value, True, matched=1)
        )
        return Resolution(locator=loc, strategy=candidate.strategy, tier=tier, attempts=attempts)

    return ResolutionFailure(attempts=attempts)
