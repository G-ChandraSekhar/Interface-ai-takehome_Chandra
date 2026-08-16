"""
Result types for policy decisions.

Kept separate from engine.py so both the discovery loop and the replay
engine can import just the types without pulling in the YAML-loading logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RiskTier(str, Enum):
    SAFE = "safe"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyCheckResult:
    decision: PolicyDecision
    risk_tier: RiskTier
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW
