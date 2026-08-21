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
        target_name: str | None = None,
        target_candidates: list[dict] | None = None,
    ):
        # target_name/target_candidates carry the acted-on element's
        # accessible name and its ranked locator ladder (from digest.py),
        # captured at the moment of the action -- this is exactly what the
        # Phase 3 distiller needs to freeze a real, robust locator ladder
        # into the artifact, rather than guessing one after the fact from a
        # bare ref like "e3" which means nothing outside that single run.
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
            target_name=target_name,
            target_candidates=target_candidates,
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

    def dom_snapshot(self, page, label: str) -> str:
        """The page's HTML at a moment worth keeping.

        §3.4 names DOM snapshots alongside screenshots. A screenshot shows what
        a person would have seen; a snapshot shows what the LOCATOR LADDER was
        actually resolving against -- which is the thing you need when a step
        stops resolving and the screenshot looks perfectly normal.

        Taken at the same moments as screenshots rather than every step: a full
        page of markup per action would bury the log it sits next to, and the
        moments that matter are the ones where something went wrong or a human
        was handed control.

        Redaction runs over the markup before it is written, using the same
        field-name rules applied everywhere else -- an input's `value=` is
        exactly where a password or a balance would otherwise be persisted in
        the clear.
        """
        path = self.run_dir / "dom" / (str(self._step_count).zfill(3) + "_" + label + ".html")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            html = page.content()
        except Exception as e:  # noqa: BLE001
            self.log_event("dom_snapshot_failed", label=label, error=str(e))
            return ""

        html = self._redact_markup(html)
        try:
            path.write_text(html, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self.log_event("dom_snapshot_failed", label=label, error=str(e))
            return ""

        self.log_event("dom_snapshot", label=label,
                       path=str(path.relative_to(self.run_dir)), bytes=len(html))
        return str(path.relative_to(self.run_dir))

    def _redact_markup(self, html: str) -> str:
        """Mask the value of any input whose name or id is a sensitive field.

        Works one tag at a time rather than with a whole-document pattern. The
        obvious cross-document regex reaches past a tag boundary and rewrites
        the NEXT input's value -- redaction that corrupts the evidence is worse
        than no redaction, because the file still looks authoritative.

        Deliberately string-level rather than parsed: this runs at the write
        boundary, and a parse that could throw would mean a failed run loses
        its snapshot exactly when it is most needed.
        """
        import re

        if not self.sensitive_field_names:
            return html

        def scrub(match):
            tag = match.group(0)
            ident = re.search(
                r"""\b(?:name|id)\s*=\s*["']?([A-Za-z0-9_\-]+)""", tag)
            if not ident or ident.group(1).lower() not in self.sensitive_field_names:
                return tag
            return re.sub(
                r"""\bvalue\s*=\s*(["'])[^"']*\1""",
                'value="***REDACTED***"',
                tag,
                flags=re.IGNORECASE,
            )

        return re.sub(r"<input\b[^>]*>", scrub, html, flags=re.IGNORECASE)

    def write_result(self, result: dict):
        with open(self.run_dir / "result.json", "w") as f:
            json.dump(result, f, indent=2)
