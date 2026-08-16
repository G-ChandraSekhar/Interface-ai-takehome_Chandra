"""
Intervention record.

Created the moment a run decides it needs a human -- carries everything an
operator needs to act without re-reading logs: which run, which
capability/goal, the current step, why it stopped, current URL, and (when
the run isn't sensitive) a screenshot. Per the brief's Section 3.6, this is
what "route an intervention request with context" means concretely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Intervention:
    run_id: str
    run_kind: str
    goal_or_capability: str
    step_id: Optional[str]
    reason: str
    page_url: str
    screenshot_path: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "run_kind": self.run_kind,
            "goal_or_capability": self.goal_or_capability,
            "step_id": self.step_id,
            "reason": self.reason,
            "page_url": self.page_url,
            "screenshot_path": self.screenshot_path,
            "created_at": self.created_at,
        }
