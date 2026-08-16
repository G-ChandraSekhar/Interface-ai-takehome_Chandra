"""
Replay result types.

The three-way split is the single most-scrutinized design point per the
brief's own glossary ("Business outcome vs. failure -- 'no such member' is a
legitimate answer the caller needs, not a crash. Conflating the two is the
most common design mistake here."). So this is genuinely three distinct
shapes, not one result object with an optional error field bolted on:

- SUCCESS: outputs were extracted, checkpoint was verified.
- BUSINESS_OUTCOME: the flow reached a known, named, *expected* terminal
  state that isn't success (member not found, permission denied). The
  caller needs to know this happened, but it is not a bug and not a crash.
- FAILURE: something genuinely went wrong -- a locator ladder was
  exhausted, an unrecoverable session condition, an application error page,
  or the checkpoint was never reached. Carries step/expected/observed detail
  because a caller (or a human debugging a run) needs to act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class FailureClass(str, Enum):
    LOCATOR_NOT_FOUND = "locator_not_found"
    APP_ERROR = "app_error"
    CHECKPOINT_NOT_MET = "checkpoint_not_met"
    INVALID_INPUT = "invalid_input"
    POLICY_DENIED = "policy_denied"
    SESSION_RECOVERY_EXHAUSTED = "session_recovery_exhausted"
    EXTRACTION_FAILED = "extraction_failed"


@dataclass
class FailureDetail:
    step_class: FailureClass
    step_id: Optional[str]
    expected: str
    observed: str


@dataclass
class StepTelemetry:
    step_id: str
    # 1-based tier in the locator ladder that actually resolved. Tier 1 is
    # the top-ranked (most semantic) candidate; anything above 1 means the
    # preferred locator no longer worked and a fallback rescued the step --
    # this is the drift signal worth watching across many production runs.
    resolved_tier: Optional[int]
    resolved_strategy: Optional[str]
    recovery_applied: bool = False
    # Set when resolution needed a fallback (tier > 1): the full
    # per-candidate account of what was rejected and why, so drift can be
    # diagnosed from telemetry alone without re-running.
    rescued_from: Optional[list] = None


@dataclass
class ReplayResult:
    status: ReplayStatus
    outputs: dict = field(default_factory=dict)
    outcome_code: Optional[str] = None
    outcome_message: Optional[str] = None
    failure: Optional[FailureDetail] = None
    step_telemetry: list = field(default_factory=list)
    run_dir: str = ""
