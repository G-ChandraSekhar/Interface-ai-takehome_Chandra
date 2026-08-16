"""
Deterministic fault injection.

The brief's core insight is that in the real environment the UI barely
drifts -- the hard part is runtime *conditions* (validation errors, session
expiry, permission denial, app errors, slow loads). A public demo site won't
reliably give you these on demand. This module lets a caller opt into one via
a `chaos` query/session parameter so replay's error handling can be tested
deterministically and reproducibly.

Supported values:
  none              -- normal behavior (default)
  session_timeout   -- injects a session-expired interstitial once, then
                        clears itself (simulates a recoverable condition)
  error500          -- the target route raises a hard 500 (simulates an
                        outright application error -- a hard failure)
  supervisor        -- the mutating action (open sub-account) requires a
                        supervisor code before it will proceed (simulates an
                        irreversible/risky action needing human confirmation)
  slow              -- the target route sleeps briefly before responding
                        (simulates transient slowness)
"""

from __future__ import annotations

import time

VALID_CHAOS = {"none", "session_timeout", "error500", "supervisor", "slow"}


def apply_chaos(chaos: str, phase: str) -> str | None:
    """
    Returns a directive string the caller should act on, or None to proceed
    normally. `phase` identifies which point in the flow is asking (e.g.
    "member_detail", "subaccount_submit") so a given chaos mode only fires
    where it makes narrative sense.
    """
    if chaos not in VALID_CHAOS:
        chaos = "none"

    if chaos == "slow" and phase in {"member_detail", "search"}:
        time.sleep(2.5)
        return None

    if chaos == "session_timeout" and phase == "member_detail":
        return "SHOW_SESSION_EXPIRED"

    if chaos == "error500" and phase == "subaccount_submit":
        return "RAISE_APP_ERROR"

    if chaos == "supervisor" and phase == "subaccount_submit":
        return "REQUIRE_SUPERVISOR_CODE"

    return None
