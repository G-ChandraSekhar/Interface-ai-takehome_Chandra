"""Tests for cross-run telemetry and drift detection.

These are deliberately built on synthetic history rather than live replays. The
question under test is "given a sequence of observations, does the assessment
reach the right conclusion" — driving a real browser to produce that sequence
would make the tests slow, flaky, and no more convincing about the logic.

The live proof that the observations themselves are recorded correctly is a
separate exercise, done by hand against the mock app.
"""

from __future__ import annotations

from src.telemetry.health import (
    STATUS_DEGRADING,
    STATUS_IMPROVED,
    STATUS_INSUFFICIENT,
    STATUS_STABLE,
    assess_health,
)
from src.telemetry.record import (
    ReplayRecord,
    StepObservation,
    append_record,
    load_records,
)


def _run(
    idx: int,
    *,
    artifact_id: str = "lookup_member_savings_balance",
    tiers=(1, 1),
    outcome: str = "success",
    strategies=("role_name", "role_name"),
) -> ReplayRecord:
    """Build one synthetic replay record.

    `recorded_at` is derived from the index so ordering is deterministic and
    does not depend on how fast the test runs.
    """
    return ReplayRecord(
        run_id=f"run-{idx:03d}",
        recorded_at=f"2026-08-{(idx % 28) + 1:02d}T09:00:00+00:00",
        artifact_id=artifact_id,
        artifact_version="1",
        tenant="tenant_a",
        outcome=outcome,
        duration_ms=1200,
        steps=[
            StepObservation(
                step_id=f"s{n + 1}",
                resolved=True,
                tier=tier,
                strategy=strategies[n] if n < len(strategies) else "role_name",
            )
            for n, tier in enumerate(tiers)
        ],
    )


def test_record_round_trips_through_the_jsonl_store(tmp_path):
    path = tmp_path / "telemetry" / "runs.jsonl"

    append_record(_run(1), path=path)
    append_record(_run(2), path=path)

    records, skipped = load_records(path)

    assert skipped == 0
    assert [r.run_id for r in records] == ["run-001", "run-002"]
    assert records[0].steps[0].strategy == "role_name"


def test_a_corrupt_line_is_skipped_and_counted_not_raised(tmp_path):
    """An append-only file written by killable processes will eventually hold a
    partial line. A health report that dies on it is worse than one that reports
    the skip and carries on with the rest of the history."""
    path = tmp_path / "runs.jsonl"

    append_record(_run(1), path=path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"run_id": "truncated-mid-wri\n')
    append_record(_run(2), path=path)

    records, skipped = load_records(path)

    assert skipped == 1
    assert len(records) == 2


def test_a_step_falling_down_the_ladder_is_reported_as_degrading():
    """The core claim. Every run here succeeds — the artifact never once fails.
    The only thing that changed is which locator strategy carried step two, and
    that alone has to be enough to raise the flag."""
    history = [_run(i, tiers=(1, 1)) for i in range(10)]
    history += [
        _run(i, tiers=(1, 3), strategies=("role_name", "css_id"))
        for i in range(10, 15)
    ]

    report = assess_health(history)
    artifact = report.artifacts[0]

    assert all(r.outcome == "success" for r in history)
    assert artifact.status == STATUS_DEGRADING
    assert report.has_degradation

    degraded = artifact.degrading_steps
    assert [s.step_id for s in degraded] == ["s2"]
    assert degraded[0].baseline_tier == 1
    assert degraded[0].recent_tier == 3
    assert degraded[0].recent_strategy == "css_id"


def test_steady_history_is_not_flagged():
    """Guards the other direction. A drift detector that flags healthy artifacts
    gets muted, and a muted detector is worth less than none at all."""
    report = assess_health([_run(i, tiers=(1, 2)) for i in range(20)])

    artifact = report.artifacts[0]
    assert artifact.status == STATUS_STABLE
    assert artifact.degrading_steps == []
    assert not report.has_degradation


def test_a_single_flaky_run_does_not_trip_the_flag():
    """One transient fallback is noise. Comparing medians rather than worst-case
    is what keeps this from paging someone over a slow page load."""
    history = [_run(i, tiers=(1, 1)) for i in range(10)]
    history += [_run(10, tiers=(1, 4))]
    history += [_run(i, tiers=(1, 1)) for i in range(11, 15)]

    artifact = assess_health(history).artifacts[0]

    assert artifact.status == STATUS_STABLE

    # The median absorbs the outlier, so nothing is acted on -- but the outlier
    # itself is still reported. A human reading this sees "typically tier 1, but
    # it hit tier 4 once", which is the right amount of alarm: none, plus a fact.
    # Discarding it entirely would be the wrong kind of quiet.
    s2 = [s for s in artifact.steps if s.step_id == "s2"][0]
    assert s2.recent_tier == 1.0
    assert s2.tier_delta == 0.0
    assert s2.worst_recent_tier == 4


def test_thin_history_reports_insufficient_data_rather_than_guessing():
    """Three runs cannot distinguish a trend from a coincidence. Saying so is
    more useful than emitting a confident number derived from nothing."""
    artifact = assess_health([_run(i) for i in range(3)]).artifacts[0]

    assert artifact.status == STATUS_INSUFFICIENT
    assert artifact.runs_seen == 3
    assert artifact.notes


def test_a_step_climbing_back_up_the_ladder_reads_as_improved():
    """After a console is fixed or an artifact is re-discovered, history should
    show recovery rather than staying permanently marked."""
    history = [_run(i, tiers=(1, 4)) for i in range(10)]
    history += [_run(i, tiers=(1, 1)) for i in range(10, 15)]

    artifact = assess_health(history).artifacts[0]
    s2 = [s for s in artifact.steps if s.step_id == "s2"][0]

    assert s2.status == STATUS_IMPROVED
    assert artifact.status == STATUS_STABLE


def test_rising_failures_without_tier_drift_point_at_the_app():
    """Not all degradation is locator drift. When the ladder is steady but runs
    start failing, the artifact is fine and something upstream is not — the
    report has to say which, or the reader will go debug the wrong thing."""
    history = [_run(i, tiers=(1, 1)) for i in range(10)]
    history += [_run(i, tiers=(1, 1), outcome="failure") for i in range(10, 15)]

    artifact = assess_health(history).artifacts[0]

    assert artifact.status == STATUS_DEGRADING
    assert artifact.degrading_steps == []
    assert artifact.recent_failure_rate == 1.0
    assert any("app" in note for note in artifact.notes)


def test_business_outcomes_are_not_counted_as_failures():
    """A lookup that correctly reports no such member is a working system giving
    a negative answer. Counting it as failure would make the health of the
    artifact depend on which member ids happened to be requested."""
    history = [_run(i, tiers=(1, 1)) for i in range(10)]
    history += [_run(i, tiers=(1, 1), outcome="business_outcome") for i in range(10, 15)]

    artifact = assess_health(history).artifacts[0]

    assert artifact.recent_failure_rate == 0.0
    assert artifact.status == STATUS_STABLE


def test_artifacts_are_assessed_independently():
    history = [_run(i, artifact_id="alpha", tiers=(1, 1)) for i in range(10)]
    history += [_run(i, artifact_id="alpha", tiers=(1, 3)) for i in range(10, 15)]
    history += [_run(i, artifact_id="beta", tiers=(1, 1)) for i in range(20)]

    report = assess_health(history)
    by_id = {a.artifact_id: a for a in report.artifacts}

    assert by_id["alpha"].status == STATUS_DEGRADING
    assert by_id["beta"].status == STATUS_STABLE
    assert [a.artifact_id for a in report.degrading] == ["alpha"]
