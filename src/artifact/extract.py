"""
Pure-text label/value extraction.

The mock target's `inner_text()` renders its legacy table markup as
"Label\\tValue" lines (confirmed directly from a live run -- see
digest.py's page_text). These functions are the two directions of the same
rule: `find_label_for_value` runs during discovery to figure out what label
was next to a value the model marked; `extract_by_label` runs during replay
to re-find the value next to that same label on whatever page replay
actually lands on. Because both directions share this one rule, an
extraction captured from one member's page correctly re-resolves against a
different member's page at replay time -- that's what makes output
parameterization real rather than a frozen replay of the discovery value.

Deliberately pure functions (str in, str out) -- no Playwright dependency --
so this logic is fully unit-testable without a browser.
"""

from __future__ import annotations

from typing import Optional


def _parse_rows(page_text: str) -> list[tuple[str, str]]:
    rows = []
    for line in page_text.splitlines():
        if "\t" in line:
            label, _, value = line.partition("\t")
            rows.append((label.strip(), value.strip()))
    return rows


def find_label_for_value(page_text: str, value: str) -> Optional[str]:
    value = value.strip()
    for label, row_value in _parse_rows(page_text):
        if row_value == value:
            return label
    return None


def extract_by_label(page_text: str, label: str) -> Optional[str]:
    label = label.strip()
    for row_label, row_value in _parse_rows(page_text):
        if row_label == label:
            return row_value
    return None
