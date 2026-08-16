"""
Tests the locator resolver's per-candidate diagnostics directly.

The value here isn't just "does it resolve" -- it's that when resolution
FAILS, the reason for every candidate is captured precisely. That's what
turns an unactionable "nothing resolved" into a failure bundle an operator
can diagnose without reproducing the run.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.replay.locator_resolver import Resolution, ResolutionFailure, resolve_locator


@dataclass
class Candidate:
    strategy: str
    value: str


class FakeLocator:
    def __init__(self, count, visible=True, enabled=True):
        self._count = count
        self._visible = visible
        self._enabled = enabled

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled


class FakePage:
    """Maps a locator string / role:name to a canned FakeLocator, so each
    rejection branch can be exercised deterministically."""

    def __init__(self, mapping, raise_on=None):
        self.mapping = mapping
        self.raise_on = raise_on or set()

    def locator(self, css):
        if css in self.raise_on:
            raise RuntimeError("bad selector")
        return self.mapping.get(css, FakeLocator(0))

    def get_by_role(self, role, name=None, exact=True):
        key = role + ":" + (name or "")
        if key in self.raise_on:
            raise RuntimeError("bad role query")
        return self.mapping.get(key, FakeLocator(0))

    def get_by_text(self, text, exact=True):
        if text in self.raise_on:
            raise RuntimeError("bad text query")
        return self.mapping.get(text, FakeLocator(0))


def test_first_tier_resolves_cleanly():
    page = FakePage({"button:Search": FakeLocator(1)})
    result = resolve_locator(page, [Candidate("role_name", "button:Search")])
    assert isinstance(result, Resolution)
    assert result.tier == 1  # 1-based
    assert result.strategy == "role_name"
    assert len(result.attempts) == 1
    assert result.attempts[0].ok is True


def test_falls_back_to_second_tier_and_records_why_the_first_failed():
    page = FakePage(
        {
            "button:Search": FakeLocator(3),  # ambiguous
            "input[name='q']": FakeLocator(1),
        }
    )
    result = resolve_locator(
        page,
        [Candidate("role_name", "button:Search"), Candidate("css_name_attr", "input[name='q']")],
    )
    assert isinstance(result, Resolution)
    assert result.tier == 2
    assert result.attempts[0].ok is False
    assert result.attempts[0].reason == "not_unique"
    assert result.attempts[0].matched == 3
    assert result.attempts[1].ok is True


def test_reports_no_match_when_zero_elements():
    page = FakePage({})
    result = resolve_locator(page, [Candidate("css_name_attr", "input[name='gone']")])
    assert isinstance(result, ResolutionFailure)
    assert result.attempts[0].reason == "no_match"
    assert result.attempts[0].matched == 0


def test_reports_not_visible():
    page = FakePage({"#hidden": FakeLocator(1, visible=False)})
    result = resolve_locator(page, [Candidate("css_id", "#hidden")])
    assert isinstance(result, ResolutionFailure)
    assert result.attempts[0].reason == "not_visible"


def test_reports_disabled():
    page = FakePage({"#off": FakeLocator(1, enabled=False)})
    result = resolve_locator(page, [Candidate("css_id", "#off")])
    assert isinstance(result, ResolutionFailure)
    assert result.attempts[0].reason == "disabled"


def test_reports_not_applicable_for_positional_marker():
    page = FakePage({})
    result = resolve_locator(page, [Candidate("positional", "no stable locator recorded")])
    assert isinstance(result, ResolutionFailure)
    assert result.attempts[0].reason == "not_applicable"


def test_reports_error_when_the_locator_call_throws():
    page = FakePage({}, raise_on={"input[name='bad']"})
    result = resolve_locator(page, [Candidate("css_name_attr", "input[name='bad']")])
    assert isinstance(result, ResolutionFailure)
    assert result.attempts[0].reason.startswith("error:")


def test_failure_summary_names_every_tier_and_reason():
    """The whole point: an operator reading the failure sees exactly what
    each candidate did, not just that the ladder was exhausted."""
    page = FakePage(
        {
            "button:Search": FakeLocator(3),
            "#missing": FakeLocator(0),
            "Search": FakeLocator(1, visible=False),
        }
    )
    result = resolve_locator(
        page,
        [
            Candidate("role_name", "button:Search"),
            Candidate("css_id", "#missing"),
            Candidate("text", "Search"),
        ],
    )
    assert isinstance(result, ResolutionFailure)
    summary = result.summary()
    assert "tier 1 role_name: not_unique (matched 3)" in summary
    assert "tier 2 css_id: no_match (matched 0)" in summary
    assert "tier 3 text: not_visible (matched 1)" in summary


def test_attempts_serialize_to_plain_dicts_for_evidence():
    page = FakePage({"#gone": FakeLocator(0)})
    result = resolve_locator(page, [Candidate("css_id", "#gone")])
    dicts = result.attempts_as_dicts()
    assert dicts == [
        {
            "tier": 1,
            "strategy": "css_id",
            "value": "#gone",
            "ok": False,
            "reason": "no_match",
            "matched": 0,
        }
    ]


def test_visibility_checks_are_best_effort_for_doubles_without_them():
    """A test double (or a surface implementation) that doesn't implement
    is_visible/is_enabled must not be treated as a rejection -- only an
    explicit False counts."""

    class MinimalLocator:
        def count(self):
            return 1

    page = FakePage({"#x": MinimalLocator()})
    result = resolve_locator(page, [Candidate("css_id", "#x")])
    assert isinstance(result, Resolution)
    assert result.tier == 1
