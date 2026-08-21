"""
DOM snapshots.

§3.4 names them alongside screenshots. A screenshot shows what a person would
have seen; a snapshot shows what the LOCATOR LADDER was resolving against --
which is the thing you need when a step stops resolving and the screenshot
looks perfectly normal.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from src.discovery.evidence import EvidenceWriter


def _writer(fields=("password", "supervisor_code", "member_id")):
    d = Path(tempfile.mkdtemp())
    (d / "screenshots").mkdir()
    return EvidenceWriter(d, set(fields)), d


class _FakePage:
    def __init__(self, html): self._html = html
    def content(self): return self._html


class _BrokenPage:
    def content(self): raise RuntimeError("page closed")


FORM = """<form>
 <input type="text" name="operator" value="teller1">
 <input type="password" name="password" value="hunter2">
 <input value="100234" id="member_id" type="text">
 <input type="text" name="memo" value="harmless">
</form>"""


def test_a_snapshot_is_written_and_logged():
    w, d = _writer()
    rel = w.dom_snapshot(_FakePage(FORM), "final_state")
    assert rel and (d / rel).exists()
    assert "dom_snapshot" in (d / "log.jsonl").read_text()


def test_sensitive_input_values_are_masked_in_the_markup():
    """An input's value= is exactly where a password or a balance would
    otherwise be persisted in the clear, in a file that gets committed."""
    w, _ = _writer()
    out = w._redact_markup(FORM)
    assert "hunter2" not in out
    assert "100234" not in out
    assert 'value="***REDACTED***"' in out


def test_redaction_does_not_corrupt_neighbouring_fields():
    """The regression that made this per-tag.

    A whole-document pattern reaches past a tag boundary and rewrites the NEXT
    input's value. Redaction that corrupts the evidence is worse than none,
    because the file still looks authoritative.
    """
    w, _ = _writer()
    out = w._redact_markup(FORM)
    assert 'name="operator" value="teller1"' in out
    assert 'name="memo" value="harmless"' in out
    assert out.count("<input") == FORM.count("<input")


def test_a_page_that_cannot_be_read_does_not_break_the_run():
    """Evidence is best-effort. A snapshot failing must never turn a real
    result into a crash -- least of all on a run that was already failing."""
    w, d = _writer()
    assert w.dom_snapshot(_BrokenPage(), "failure") == ""
    assert "dom_snapshot_failed" in (d / "log.jsonl").read_text()


def test_a_successful_run_still_carries_evidence():
    """Capturing only failures optimised for debugging and forgot inspection.

    A reviewer opens the run that WORKED first. An empty evidence panel there
    makes the system look less accountable than it is -- and a healthy page's
    markup is the baseline every later failure gets compared to.
    """
    import inspect
    from src.replay import engine

    source = inspect.getsource(engine._finish)
    before_capture = source.split("evidence.screenshot(")[0]
    assert "ReplayStatus.SUCCESS" not in before_capture, (
        "evidence capture must not sit behind a status guard"
    )


def test_snapshots_are_taken_wherever_screenshots_are():
    """Paired on purpose: the moments worth a picture are the moments worth
    the markup. Every step would bury the log it sits beside."""
    from src.discovery import loop
    from src.replay import engine

    for module in (loop, engine):
        source = inspect.getsource(module)
        assert source.count("evidence.dom_snapshot(") >= 2, module.__name__
        # Never more snapshots than screenshots -- that would mean one is
        # firing somewhere a screenshot is not.
        assert source.count("evidence.dom_snapshot(") <= source.count("evidence.screenshot(")


# ---------------------------------------------------------------------------
# Serving a snapshot
# ---------------------------------------------------------------------------


def test_a_snapshot_that_exists_is_actually_served():
    """The test that was missing.

    There was a traversal test, and it passed -- because it hits a path that
    returns 404 BEFORE the response object is ever constructed. The endpoint
    referenced PlainTextResponse without importing it, so every real fetch
    500'd and nothing noticed. A test that only exercises the refusal proves
    the guard, not the feature.
    """
    import json

    from fastapi.testclient import TestClient

    import src.capability_api.server as server
    from src.capability_api.runs import evidence_root, list_runs

    root = evidence_root()
    run = next(
        (r for r in list_runs(limit=200)
         if (root / r["run_id"] / "dom").is_dir()
         and any((root / r["run_id"] / "dom").glob("*.html"))),
        None,
    )
    if run is None:
        pytest.skip("no committed run carries a DOM snapshot yet")

    name = sorted(p.name for p in (root / run["run_id"] / "dom").glob("*.html"))[0]
    response = TestClient(server.app).get(
        "/runs/" + run["run_id"] + "/dom/" + name
    )

    assert response.status_code == 200, response.text[:200]
    # Served as text, never as a live page: the snapshot is a bank page with
    # forms and scripts, and rendering it would run the target's markup in the
    # reviewer's browser.
    assert response.headers["content-type"].startswith("text/plain")
    assert "<" in response.text


def test_the_snapshot_endpoint_refuses_traversal():
    from fastapi.testclient import TestClient

    import src.capability_api.server as server

    r = TestClient(server.app).get("/runs/whatever/dom/..%2f..%2fsecret")
    assert r.status_code == 404
