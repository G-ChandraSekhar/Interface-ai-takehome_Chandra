"""
Stability scoring: replay the same artifact+params N times and aggregate how
reliably it succeeds and how far its locator resolution is drifting from
top-tier. Corresponds to the assignment's "Multi-run stability" stretch goal
(Section 8), combined with the confidence half of "Confidence & approval" --
a signal an operator can look at before deciding whether an artifact is
trustworthy enough to approve for unattended replay.

Deliberately sequential, not parallelized, and not new infrastructure: this
reuses replay_artifact() exactly as it already exists, N times in a loop.
Section 7 of the brief is explicit that building scaling infrastructure
(queues, clusters) is not rewarded here -- this is a small research tool an
operator runs occasionally before approving a capability, not a production
hot path that would need one.

Relationship to `cli.py health`: this command asks "is this artifact reliable
right now", by running it N times back to back. That is a snapshot, and a
snapshot cannot see a step that used to resolve at tier 1 and now resolves at
tier 3. `health` reads the same telemetry across time instead of across one
sitting. The two answer different questions and neither replaces the other.

IMPORTANT: this module never sets Artifact.approved. See
ArtifactStability's docstring in src/artifact/schema.py for why -- approval
stays a human reviewer's out-of-band decision everywhere in this codebase;
a computed score informs that decision without making it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.artifact.schema import Artifact, ArtifactStability
from src.replay.engine import replay_artifact
from src.replay.result import ReplayStatus
from src.telemetry.record import SOURCE_STABILITY


@dataclass
class StabilityReport:
    runs: int
    successes: int = 0
    business_outcomes: int = 0
    failures: int = 0
    # failure class value -> count, e.g. {"locator_not_found": 1}
    failure_classes: dict = field(default_factory=dict)
    step_avg_tier: dict = field(default_factory=dict)
    step_worst_tier: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def business_outcome_rate(self) -> float:
        return self.business_outcomes / self.runs if self.runs else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.0

    def to_artifact_stability(self) -> ArtifactStability:
        return ArtifactStability(
            sample_size=self.runs,
            success_rate=self.success_rate,
            business_outcome_rate=self.business_outcome_rate,
            failure_rate=self.failure_rate,
            step_avg_tier=self.step_avg_tier,
            step_worst_tier=self.step_worst_tier,
            computed_at=datetime.now(timezone.utc),
        )


def run_stability(
    artifact: Artifact,
    params: dict,
    n: int,
    *,
    page_factory=None,
    evidence_root=None,
    run_id_prefix: str = "stability",
    **replay_kwargs,
) -> StabilityReport:
    """Replays `artifact` against `params` n times sequentially, aggregating
    outcomes and per-step locator-tier telemetry.

    page_factory: optional callable() -> page. Real usage (the CLI) passes
    none, letting each replay_artifact() call own and launch its own
    browser, exactly like running `replay` by hand n times -- simple,
    correct, and it's what a human operator would otherwise do manually.
    Tests supply a page_factory returning a fresh FakePage per iteration so
    each run's simulated page state doesn't leak into the next.
    """
    if n < 1:
        raise ValueError("n must be at least 1")

    successes = 0
    business_outcomes = 0
    failures = 0
    failure_classes: Counter = Counter()
    step_tiers: dict = {}

    for i in range(n):
        run_id = run_id_prefix + "_" + str(i + 1) + "of" + str(n)
        page = page_factory() if page_factory else None
        result = replay_artifact(
            artifact,
            params,
            page=page,
            evidence_root=evidence_root,
            run_id=run_id,
            # Tagged so `health` can exclude these by default. N runs fired
            # back to back in one minute would otherwise dominate a baseline
            # window meant to span weeks, and the drift signal would end up
            # measuring the operator's testing habits rather than the bank's
            # console.
            telemetry_source=SOURCE_STABILITY,
            **replay_kwargs,
        )
        if result.status == ReplayStatus.SUCCESS:
            successes += 1
        elif result.status == ReplayStatus.BUSINESS_OUTCOME:
            business_outcomes += 1
        else:
            failures += 1
            if result.failure:
                failure_classes[result.failure.step_class.value] += 1

        for t in result.step_telemetry:
            step_tiers.setdefault(t.step_id, []).append(t.resolved_tier)

    step_avg_tier = {sid: sum(tiers) / len(tiers) for sid, tiers in step_tiers.items()}
    step_worst_tier = {sid: max(tiers) for sid, tiers in step_tiers.items()}

    return StabilityReport(
        runs=n,
        successes=successes,
        business_outcomes=business_outcomes,
        failures=failures,
        failure_classes=dict(failure_classes),
        step_avg_tier=step_avg_tier,
        step_worst_tier=step_worst_tier,
    )
