"""Append-only telemetry for replay runs.

Why this exists
---------------
`stability.py` answers "is this artifact reliable *right now*", by replaying it N
times back to back. That is a snapshot. It cannot answer the question that
actually matters once a fleet of artifacts is in production: "which of these is
quietly getting worse?"

A step that starts resolving at a lower-priority locator strategy is telling you
it will break soon, while it is still passing. That signal only exists across
time, so it needs somewhere durable to accumulate. This module is that place.

Design notes
------------
- JSON Lines, append-only. A replay never rewrites history and never needs a
  read-modify-write cycle, so concurrent replays cannot corrupt each other's
  records the way a single JSON array would.
- Records are self-describing primitives, not references into the artifact
  schema. History has to stay readable after an artifact is edited or deleted;
  if a record pointed at a live artifact, the past would change when the present
  did.
- Reads tolerate malformed lines. An append-only file written by processes that
  can be killed mid-write will eventually contain a partial line, and a health
  report that crashes on one bad byte is worse than one that reports the skip.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

DEFAULT_TELEMETRY_PATH = Path("telemetry") / "runs.jsonl"

# Runs produced by the `stability` command are tagged separately from ordinary
# replays. N runs fired back to back in one minute would otherwise dominate a
# baseline window meant to span weeks, and the drift signal would end up
# measuring the operator's testing habits rather than the bank's console.
SOURCE_REPLAY = "replay"
SOURCE_STABILITY = "stability"


class StepObservation(BaseModel):
    """What actually happened when one artifact step was resolved and executed.

    `tier` is the 1-indexed position in the step's declared locator ladder that
    finally resolved. Tier 1 means the preferred strategy worked. A higher number
    means every strategy above it was rejected, which is the drift signal.
    """

    step_id: str
    resolved: bool
    tier: Optional[int] = None
    strategy: Optional[str] = None
    declared_confidence: Optional[float] = None
    candidates_tried: Optional[int] = None
    failure_reason: Optional[str] = None


class ReplayRecord(BaseModel):
    """One replay invocation, start to finish."""

    run_id: str
    recorded_at: str
    artifact_id: str
    artifact_version: Optional[str] = None
    tenant: Optional[str] = None
    source: str = SOURCE_REPLAY

    outcome: str  # success | business_outcome | failure | blocked
    outcome_detail: Optional[str] = None
    duration_ms: Optional[int] = None

    steps: List[StepObservation] = Field(default_factory=list)

    @staticmethod
    def utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_record(
    record: ReplayRecord,
    path: Path = DEFAULT_TELEMETRY_PATH,
) -> Path:
    """Append one record. Creates the parent directory on first write.

    Opened in append mode with an explicit flush so a record is durable as soon
    as the replay that produced it finishes, rather than whenever the process
    happens to exit.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record.model_dump(exclude_none=True), separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def load_records(
    path: Path = DEFAULT_TELEMETRY_PATH,
    artifact_id: Optional[str] = None,
    sources: Optional[Sequence[str]] = (SOURCE_REPLAY,),
) -> Tuple[List[ReplayRecord], int]:
    """Read history. Returns (records, skipped_line_count).

    Skipped lines are counted rather than raised. The caller is expected to
    surface the count so a silently truncating file does not silently truncate
    the report as well.
    """
    path = Path(path)
    if not path.exists():
        return [], 0

    records: List[ReplayRecord] = []
    skipped = 0

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(ReplayRecord(**json.loads(line)))
            except Exception:
                skipped += 1

    if artifact_id is not None:
        records = [r for r in records if r.artifact_id == artifact_id]
    if sources is not None:
        records = [r for r in records if r.source in sources]

    records.sort(key=lambda r: r.recorded_at)
    return records, skipped


def group_by_artifact(records: Iterable[ReplayRecord]) -> dict:
    """Bucket records by artifact id, preserving chronological order."""
    grouped: dict = {}
    for record in records:
        grouped.setdefault(record.artifact_id, []).append(record)
    for runs in grouped.values():
        runs.sort(key=lambda r: r.recorded_at)
    return grouped
