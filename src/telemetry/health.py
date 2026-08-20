"""Drift detection across replay history.

The claim this module makes good on
-----------------------------------
A ranked locator ladder is often defended as resilience: if the preferred
strategy stops working, a fallback catches it. That is true but it is the less
interesting half. The more interesting half is that a fallback *catching* the
step is itself information — the page changed, the step survived, and nothing
failed. With a single selector you would have found out by failing in
production. With a ladder you can find out by succeeding at tier two.

But only if someone compares against how the step used to resolve. That
comparison is what lives here.

How the comparison works
------------------------
Two disjoint windows per artifact: the earliest runs form a baseline, the most
recent runs form the current picture. For each step, take the median resolved
tier in each window and compare.

Median rather than mean: tiers are ordinal positions in a ladder, not a
quantity, and the average of tier 1 and tier 5 is not "tier 3" in any meaningful
sense. Median also shrugs off a single flaky run, which matters because one
transient failure should not page anyone.

Disjoint rather than sliding: if the windows overlap, a change has to be large
enough to move both sides before it registers, which is exactly backwards.

What this deliberately does not do
----------------------------------
It does not modify artifacts, and it does not touch `approved`. Degradation is
evidence for a human decision about whether a capability should keep running
unattended. Turning it into an automatic gate would change what approval means,
from "someone reviewed this" to "the numbers looked fine" — and those are not
the same claim to make to a regulator.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from .record import ReplayRecord, group_by_artifact

# Windows are small on purpose. These are expensive browser runs, not request
# logs; an artifact with a hundred runs of history is already unusual.
DEFAULT_BASELINE_N = 10
DEFAULT_RECENT_N = 5
DEFAULT_MIN_RUNS = 6

# One full rung down the ladder. Half-steps happen naturally when a window has
# an even number of runs split across two tiers, and are not worth flagging.
TIER_DELTA_THRESHOLD = 1.0

# Outcome rates move for reasons that have nothing to do with drift (different
# input parameters, a genuinely absent record), so this is set loose.
FAILURE_RATE_THRESHOLD = 0.20

STATUS_STABLE = "stable"
STATUS_DEGRADING = "degrading"
STATUS_IMPROVED = "improved"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_NEW_STEP = "no_baseline"


class StepHealth(BaseModel):
    step_id: str
    status: str
    baseline_tier: Optional[float] = None
    recent_tier: Optional[float] = None
    tier_delta: Optional[float] = None
    worst_recent_tier: Optional[int] = None
    recent_strategy: Optional[str] = None
    baseline_strategy: Optional[str] = None


class ArtifactHealth(BaseModel):
    artifact_id: str
    status: str
    runs_seen: int
    baseline_runs: int = 0
    recent_runs: int = 0

    baseline_failure_rate: Optional[float] = None
    recent_failure_rate: Optional[float] = None
    failure_rate_delta: Optional[float] = None

    steps: List[StepHealth] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    @property
    def degrading_steps(self) -> List[StepHealth]:
        return [s for s in self.steps if s.status == STATUS_DEGRADING]


class HealthReport(BaseModel):
    generated_at: str
    artifacts: List[ArtifactHealth] = Field(default_factory=list)
    records_read: int = 0
    records_skipped: int = 0

    @property
    def degrading(self) -> List[ArtifactHealth]:
        return [a for a in self.artifacts if a.status == STATUS_DEGRADING]

    @property
    def has_degradation(self) -> bool:
        return bool(self.degrading)


def _median_tier(records: Sequence[ReplayRecord], step_id: str) -> Optional[float]:
    tiers = [
        obs.tier
        for record in records
        for obs in record.steps
        if obs.step_id == step_id and obs.resolved and obs.tier is not None
    ]
    return statistics.median(tiers) if tiers else None


def _worst_tier(records: Sequence[ReplayRecord], step_id: str) -> Optional[int]:
    tiers = [
        obs.tier
        for record in records
        for obs in record.steps
        if obs.step_id == step_id and obs.resolved and obs.tier is not None
    ]
    return max(tiers) if tiers else None


def _last_strategy(records: Sequence[ReplayRecord], step_id: str) -> Optional[str]:
    for record in reversed(records):
        for obs in record.steps:
            if obs.step_id == step_id and obs.resolved and obs.strategy:
                return obs.strategy
    return None


def _failure_rate(records: Sequence[ReplayRecord]) -> Optional[float]:
    """Fraction of runs that failed outright.

    `business_outcome` is not counted as a failure. A lookup that correctly
    reports no such member is a working system returning a negative answer, and
    folding that into a failure rate would make the number describe the input
    data rather than the artifact.
    """
    if not records:
        return None
    failures = sum(1 for r in records if r.outcome in ("failure", "blocked"))
    return failures / len(records)


def _step_ids(records: Sequence[ReplayRecord]) -> List[str]:
    seen: List[str] = []
    for record in records:
        for obs in record.steps:
            if obs.step_id not in seen:
                seen.append(obs.step_id)
    return seen


def _split_windows(
    runs: Sequence[ReplayRecord],
    baseline_n: int,
    recent_n: int,
):
    """Carve disjoint baseline and recent windows out of one artifact's history.

    When there is not enough history for both full windows, the recent window is
    honoured first and the baseline takes whatever remains. A short baseline is
    still a real comparison; a contaminated one is not.
    """
    recent = list(runs[-recent_n:])
    remaining = list(runs[: max(0, len(runs) - len(recent))])
    baseline = remaining[:baseline_n]
    return baseline, recent


def assess_artifact(
    artifact_id: str,
    runs: Sequence[ReplayRecord],
    *,
    baseline_n: int = DEFAULT_BASELINE_N,
    recent_n: int = DEFAULT_RECENT_N,
    min_runs: int = DEFAULT_MIN_RUNS,
    tier_delta_threshold: float = TIER_DELTA_THRESHOLD,
    failure_rate_threshold: float = FAILURE_RATE_THRESHOLD,
) -> ArtifactHealth:
    if len(runs) < min_runs:
        return ArtifactHealth(
            artifact_id=artifact_id,
            status=STATUS_INSUFFICIENT,
            runs_seen=len(runs),
            notes=[f"needs at least {min_runs} runs, has {len(runs)}"],
        )

    baseline, recent = _split_windows(runs, baseline_n, recent_n)

    if not baseline:
        return ArtifactHealth(
            artifact_id=artifact_id,
            status=STATUS_INSUFFICIENT,
            runs_seen=len(runs),
            recent_runs=len(recent),
            notes=["no runs left for a baseline window after reserving recent runs"],
        )

    steps: List[StepHealth] = []
    notes: List[str] = []

    for step_id in _step_ids(runs):
        base_tier = _median_tier(baseline, step_id)
        recent_tier = _median_tier(recent, step_id)

        if recent_tier is None:
            steps.append(
                StepHealth(
                    step_id=step_id,
                    status=STATUS_INSUFFICIENT,
                    baseline_tier=base_tier,
                )
            )
            notes.append(f"{step_id}: never resolved in the recent window")
            continue

        if base_tier is None:
            steps.append(
                StepHealth(
                    step_id=step_id,
                    status=STATUS_NEW_STEP,
                    recent_tier=recent_tier,
                    worst_recent_tier=_worst_tier(recent, step_id),
                    recent_strategy=_last_strategy(recent, step_id),
                )
            )
            continue

        delta = recent_tier - base_tier
        if delta >= tier_delta_threshold:
            status = STATUS_DEGRADING
        elif delta <= -tier_delta_threshold:
            status = STATUS_IMPROVED
        else:
            status = STATUS_STABLE

        steps.append(
            StepHealth(
                step_id=step_id,
                status=status,
                baseline_tier=base_tier,
                recent_tier=recent_tier,
                tier_delta=delta,
                worst_recent_tier=_worst_tier(recent, step_id),
                baseline_strategy=_last_strategy(baseline, step_id),
                recent_strategy=_last_strategy(recent, step_id),
            )
        )

    base_fail = _failure_rate(baseline)
    recent_fail = _failure_rate(recent)
    fail_delta = (
        recent_fail - base_fail
        if base_fail is not None and recent_fail is not None
        else None
    )

    status = STATUS_STABLE
    if any(s.status == STATUS_DEGRADING for s in steps):
        status = STATUS_DEGRADING
    elif fail_delta is not None and fail_delta >= failure_rate_threshold:
        status = STATUS_DEGRADING
        notes.append(
            f"failure rate rose from {base_fail:.0%} to {recent_fail:.0%} "
            "without a locator tier change — look at the app, not the artifact"
        )

    return ArtifactHealth(
        artifact_id=artifact_id,
        status=status,
        runs_seen=len(runs),
        baseline_runs=len(baseline),
        recent_runs=len(recent),
        baseline_failure_rate=base_fail,
        recent_failure_rate=recent_fail,
        failure_rate_delta=fail_delta,
        steps=steps,
        notes=notes,
    )


def assess_health(
    records: Sequence[ReplayRecord],
    *,
    baseline_n: int = DEFAULT_BASELINE_N,
    recent_n: int = DEFAULT_RECENT_N,
    min_runs: int = DEFAULT_MIN_RUNS,
    records_skipped: int = 0,
) -> HealthReport:
    grouped: Dict[str, List[ReplayRecord]] = group_by_artifact(records)

    artifacts = [
        assess_artifact(
            artifact_id,
            runs,
            baseline_n=baseline_n,
            recent_n=recent_n,
            min_runs=min_runs,
        )
        for artifact_id, runs in sorted(grouped.items())
    ]

    return HealthReport(
        generated_at=ReplayRecord.utcnow_iso(),
        artifacts=artifacts,
        records_read=len(records),
        records_skipped=records_skipped,
    )
