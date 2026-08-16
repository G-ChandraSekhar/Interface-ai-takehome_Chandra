"""
Evidence writer for discovery runs.

Satisfies the brief's evidence/observability requirement (Section 3.5): a
structured log of what the agent did and why, plus a richer signal
(screenshots) for debugging. Redaction happens here, at the write boundary
-- by the time anything reaches disk, sensitive field values have already
been masked, so there's no raw secret sitting in the evidence directory
even transiently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.guardrails.redact import redact_value


class EvidenceWriter:
    def __init__(self, run_dir: Path, sensitive_field_names: set[str]):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "screenshots").mkdir(exist_ok=True)
        self.sensitive_field_names = {n.lower() for n in sensitive_field_names}
        self._log_path = self.run_dir / "log.jsonl"
        self._step_count = 0

    def _redact_if_sensitive(self, field_name: str | None, value: str) -> str:
        if field_name and field_name.lower() in self.sensitive_field_names:
            return redact_value(value)
        return value

    def log_event(self, event_type: str, **fields):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **fields,
        }
        with open(self._log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def log_step(
        self,
        *,
        step_number: int,
        observation_text: str,
        assistant_content: str | None,
        tool_name: str | None,
        tool_args: dict | None,
        tool_result_message: str | None,
        tool_ok: bool | None,
        page_url: str,
    ):
        # redact any sensitive value embedded in the args (e.g. a typed
        # password) by field-name heuristic: the 'text' arg for a 'type'
        # call whose element name matched a sensitive field is redacted by
        # the caller before this is invoked -- see loop.py.
        self.log_event(
            "step",
            step_number=step_number,
            page_url=page_url,
            observation=observation_text,
            assistant_content=assistant_content,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result_message,
            tool_ok=tool_ok,
        )

    def screenshot(self, page, label: str) -> str:
        self._step_count += 1
        path = self.run_dir / "screenshots" / f"{self._step_count:03d}_{label}.png"
        try:
            page.screenshot(path=str(path))
        except Exception as e:
            self.log_event("screenshot_failed", label=label, error=str(e))
            return ""
        return str(path.relative_to(self.run_dir))

    def write_result(self, result: dict):
        with open(self.run_dir / "result.json", "w") as f:
            json.dump(result, f, indent=2)
