"""
Policy engine.

Both the discovery loop (src/discovery/) and the replay engine
(src/replay/) call check_action() before every single action -- click,
type, navigate, whatever. Neither path is trusted to police itself; this
module is the one place the allowlist is enforced, per the assignment
brief's requirement (Section 3.4) that the agent "must not act outside" the
configured allowlist.

Design choices, and why:

- Origin allowlist is a hard DENY, no exceptions -- there's no scenario in
  this system where acting on an un-allowlisted origin is acceptable, so we
  don't expose a confirmation path for it at all.
- Risk tiers (safe / mutating / irreversible) are looked up from the most
  specific pattern list first (irreversible, then mutating, then safe) so a
  route that happens to match a broader "safe" pattern doesn't accidentally
  shadow a narrower irreversible one.
- SAFE actions always proceed. MUTATING actions require either an approved
  artifact (the production replay path) or an explicit per-run confirmation
  flag (the discovery path, where a human deliberately opted into a
  mutating run). IRREVERSIBLE actions additionally require a live
  confirmation regardless of artifact approval -- an approved artifact is a
  reviewer's sign-off on the *capability*, not standing authorization to
  skip confirmation on its most consequential step every time it runs.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from urllib.parse import urlparse

import yaml

from src.guardrails.result import PolicyCheckResult, PolicyDecision, RiskTier

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "allowlist.yaml"


class PolicyEngine:
    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)

        self.allowed_origins: set[str] = set(cfg.get("allowed_origins", []))
        self.allowed_action_types: set[str] = set(cfg.get("allowed_action_types", []))
        self.safe_patterns: list[str] = cfg.get("safe_route_patterns", [])
        self.mutating_patterns: list[str] = cfg.get("mutating_route_patterns", [])
        self.irreversible_patterns: list[str] = cfg.get("irreversible_route_patterns", [])
        limits = cfg.get("limits", {})
        self.max_steps_per_run: int = limits.get("max_steps_per_run", 40)
        self.max_run_duration_seconds: int = limits.get("max_run_duration_seconds", 300)
        self.max_recovery_attempts_per_step: int = limits.get(
            "max_recovery_attempts_per_step", 2
        )
        self.sensitive_field_names: set[str] = set(cfg.get("sensitive_field_names", []))
        self.sensitive_output_fields: set[str] = set(cfg.get("sensitive_output_fields", []))

    def _origin_of(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _path_of(self, url: str) -> str:
        return urlparse(url).path

    def _risk_tier_for_path(self, path: str) -> RiskTier:
        if any(fnmatch.fnmatch(path, pat) for pat in self.irreversible_patterns):
            return RiskTier.IRREVERSIBLE
        if any(fnmatch.fnmatch(path, pat) for pat in self.mutating_patterns):
            return RiskTier.MUTATING
        if any(fnmatch.fnmatch(path, pat) for pat in self.safe_patterns):
            return RiskTier.SAFE
        # Unknown route: treat conservatively as mutating rather than safe,
        # since we have no evidence it's read-only.
        return RiskTier.MUTATING

    def check_origin(self, url: str) -> PolicyCheckResult:
        origin = self._origin_of(url)
        if origin not in self.allowed_origins:
            return PolicyCheckResult(
                decision=PolicyDecision.DENY,
                risk_tier=RiskTier.IRREVERSIBLE,
                reason=f"Origin '{origin}' is not in the configured allowlist.",
            )
        return PolicyCheckResult(
            decision=PolicyDecision.ALLOW,
            risk_tier=RiskTier.SAFE,
            reason="Origin allowed.",
        )

    def check_action(
        self,
        action_type: str,
        url: str,
        *,
        confirmed: bool = False,
        artifact_approved: bool = False,
    ) -> PolicyCheckResult:
        origin_check = self.check_origin(url)
        if not origin_check.allowed:
            return origin_check

        if action_type not in self.allowed_action_types:
            return PolicyCheckResult(
                decision=PolicyDecision.DENY,
                risk_tier=RiskTier.IRREVERSIBLE,
                reason=f"Action type '{action_type}' is not in the allowed action types.",
            )

        path = self._path_of(url)
        risk_tier = self._risk_tier_for_path(path)

        if risk_tier == RiskTier.SAFE:
            return PolicyCheckResult(
                decision=PolicyDecision.ALLOW,
                risk_tier=risk_tier,
                reason="Safe/reversible action.",
            )

        if risk_tier == RiskTier.MUTATING:
            if artifact_approved or confirmed:
                return PolicyCheckResult(
                    decision=PolicyDecision.ALLOW,
                    risk_tier=risk_tier,
                    reason="Mutating action allowed: approved artifact or explicit confirmation.",
                )
            return PolicyCheckResult(
                decision=PolicyDecision.REQUIRE_CONFIRMATION,
                risk_tier=risk_tier,
                reason="Mutating action requires an approved artifact or explicit confirmation.",
            )

        # IRREVERSIBLE -- never runs unattended, under any flag.
        #
        # This is deliberately absolute rather than flag-gated. An earlier
        # version accepted a `confirmed=True` CLI flag here, which made the
        # safety property depend on how the tool happened to be invoked. In
        # a banking context the stronger and more defensible guarantee is
        # structural: an irreversible action in this system cannot execute
        # without a human taking control of the live session at the moment
        # it happens. Replay therefore routes this to the escalation path
        # (Phase 6) rather than treating it as a failure -- see
        # src/replay/engine.py.
        #
        # Note `confirmed` is intentionally ignored for this tier. It still
        # governs the MUTATING tier above, where an operator authorizing a
        # whole run in advance is reasonable.
        return PolicyCheckResult(
            decision=PolicyDecision.REQUIRE_CONFIRMATION,
            risk_tier=risk_tier,
            reason=(
                "Irreversible action requires a human to take control of the live "
                "session -- it can never run unattended, regardless of artifact "
                "approval status or confirmation flags."
            ),
        )

    def check_budget(self, step_count: int, elapsed_seconds: float) -> PolicyCheckResult:
        if step_count > self.max_steps_per_run:
            return PolicyCheckResult(
                decision=PolicyDecision.DENY,
                risk_tier=RiskTier.SAFE,
                reason=f"Exceeded max_steps_per_run ({self.max_steps_per_run}).",
            )
        if elapsed_seconds > self.max_run_duration_seconds:
            return PolicyCheckResult(
                decision=PolicyDecision.DENY,
                risk_tier=RiskTier.SAFE,
                reason=f"Exceeded max_run_duration_seconds ({self.max_run_duration_seconds}).",
            )
        return PolicyCheckResult(
            decision=PolicyDecision.ALLOW, risk_tier=RiskTier.SAFE, reason="Within budget."
        )
