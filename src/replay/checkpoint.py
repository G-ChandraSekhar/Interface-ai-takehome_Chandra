"""
Checkpoint matching.

A checkpoint answers one question -- *did this run end where the capability
claims it ends* -- and it answers it in two parts, because on a real target
those parts are not always both knowable from the caller's inputs.

**The URL pattern** is rendered against this invocation's parameters and
compared to the path replay actually finished on. Whenever the destination is
predictable from the inputs, this is the whole story: search for member 100987
and the flow ends at `/members/100987`, which is the number the caller passed.

**Assertions** exist for when it is not. Search by last name ends wherever the
search resolved -- a member number the caller does not know and cannot be asked
for. The distiller has nothing to parameterise, so it would otherwise freeze the
literal it happened to observe, producing an artifact that replays for exactly
one surname.

For those, the URL pattern carries a `{*}` segment (shape) and an assertion
carries the identity: *the value extracted for `member_name` must contain the
`query` the caller searched for*. That is the same division `ExtractionRule`
already makes -- assert the shape, parameterise the identity.

`{*}` matches exactly one path segment, deliberately. `fnmatch`'s `*` crosses
`/`, so a checkpoint of `/members/*` would be satisfied by
`/members/100987/transfer` -- a run that died halfway through a transfer form.
Measured, not assumed.

Pure string logic, no Playwright dependency, so all of it is directly
unit-testable.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

WILDCARD = "{*}"

# {name}, but never {*} -- that is a wildcard, not a parameter reference.
_PARAM_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_url_pattern(pattern, params):
    """Substitute {param} references, leaving any {*} wildcard intact."""

    def substitute(match):
        name = match.group(1)
        if name not in params:
            raise ValueError("Checkpoint pattern references unknown param: '" + name + "'")
        return str(params[name])

    return _PARAM_REF.sub(substitute, pattern)


def _paths_match(expected_path, actual_path):
    if WILDCARD not in expected_path:
        # Unchanged behaviour for every checkpoint recorded before wildcards
        # existed: an exact comparison.
        return expected_path == actual_path

    expected_segments = expected_path.split("/")
    actual_segments = actual_path.split("/")
    if len(expected_segments) != len(actual_segments):
        return False
    return all(
        expected == WILDCARD or expected == actual
        for expected, actual in zip(expected_segments, actual_segments)
    )


def assertion_met(assertion, outputs, params):
    """Does one content assertion hold? Returns (ok, reason).

    A missing output or a missing parameter is a failure rather than a skip:
    an assertion that quietly does not run is worse than no assertion, because
    the artifact still claims to make it.
    """
    # Out of its declared mode, the claim simply does not apply. That is not
    # the same as an assertion quietly failing to run: the condition is in
    # the artifact, where a reviewer approving it can see exactly when it
    # binds and when it does not.
    for param, expected_mode in (assertion.when or {}).items():
        if str(params.get(param, "")).lower() != str(expected_mode).lower():
            return True, ""

    if assertion.output not in outputs:
        return False, "output '" + assertion.output + "' was not extracted"

    actual = str(outputs[assertion.output])

    if assertion.contains_input is not None:
        if assertion.contains_input not in params:
            return False, "param '" + assertion.contains_input + "' not supplied"
        expected = str(params[assertion.contains_input])
    elif assertion.contains_literal is not None:
        expected = assertion.contains_literal
    else:
        return False, "assertion declares neither contains_input nor contains_literal"

    haystack, needle = (actual, expected)
    if not assertion.case_sensitive:
        haystack, needle = haystack.lower(), needle.lower()

    if needle in haystack:
        return True, ""
    return False, (
        "extracted " + assertion.output + "=" + repr(actual)
        + " does not contain " + repr(expected)
    )


def assertions_met(assertions, outputs, params):
    """All content assertions. Returns (ok, first_failure_reason)."""
    for assertion in assertions or []:
        ok, reason = assertion_met(assertion, outputs, params)
        if not ok:
            return False, reason
    return True, ""


def checkpoint_met(pattern, params, actual_url):
    expected_path = render_url_pattern(pattern, params)
    actual_path = urlparse(actual_url).path
    return _paths_match(expected_path, actual_path)
