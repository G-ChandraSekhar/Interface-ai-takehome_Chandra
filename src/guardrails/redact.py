"""
Redaction.

Called before anything is written to a log, artifact, or evidence file.
Rather than trying to detect sensitive *values* (which is unreliable), we
redact by *field name* -- the caller tells us the field name a value came
from (e.g. "password", "supervisor_code") and we mask it if that name is on
the configured sensitive list. This is simpler and more predictable than
value-pattern heuristics, at the cost of needing the sensitive field list to
be kept current -- an explicit, documented trade-off (see REPORT.md).

Redaction fully replaces the value with a fixed placeholder rather than
partially masking it (e.g. showing the first/last character). Partial
masking still leaks information -- the value's length, its first and last
characters -- which for something like a short numeric balance or a 4-digit
code can narrow it down considerably. A fixed placeholder leaks nothing.
"""

from __future__ import annotations

REDACTED_PLACEHOLDER = "***REDACTED***"


def redact_value(value: str) -> str:
    if not value:
        return value
    return REDACTED_PLACEHOLDER


def redact_fields(data: dict, sensitive_field_names: set[str]) -> dict:
    """Returns a new dict with any key in sensitive_field_names masked.
    Does not mutate the input."""
    redacted = {}
    for key, value in data.items():
        if key.lower() in {name.lower() for name in sensitive_field_names}:
            redacted[key] = redact_value(str(value)) if value is not None else value
        else:
            redacted[key] = value
    return redacted
