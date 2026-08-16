from __future__ import annotations

from src.artifact.extract import extract_by_label, find_label_for_value

PAGE_TEXT = (
    "CorePoint Teller Desk\n\n"
    "Member Record\n\n"
    "Member Name\tDana Whitfield\n"
    "Member ID\t4521\n"
    "Regular Savings\t2,410.55\n"
    "Status\tActive\n\n"
    "Open Sub-Account\n"
)

PAGE_TEXT_DIFFERENT_MEMBER = (
    "CorePoint Teller Desk\n\n"
    "Member Record\n\n"
    "Member Name\tMarcus Ojo\n"
    "Member ID\t8832\n"
    "Regular Savings\t918.20\n"
    "Status\tActive\n\n"
    "Open Sub-Account\n"
)


def test_find_label_for_value_matches_exact_row():
    assert find_label_for_value(PAGE_TEXT, "Dana Whitfield") == "Member Name"
    assert find_label_for_value(PAGE_TEXT, "2,410.55") == "Regular Savings"


def test_find_label_for_value_returns_none_when_absent():
    assert find_label_for_value(PAGE_TEXT, "Not On This Page") is None


def test_extract_by_label_finds_current_row_value():
    assert extract_by_label(PAGE_TEXT, "Member Name") == "Dana Whitfield"
    assert extract_by_label(PAGE_TEXT, "Regular Savings") == "2,410.55"


def test_extraction_rule_is_reusable_across_different_pages():
    """The exact scenario that makes parameterization real: a label captured
    from one member's page correctly re-extracts a DIFFERENT value from a
    different member's page."""
    label = find_label_for_value(PAGE_TEXT, "2,410.55")
    assert label == "Regular Savings"

    # Same label, different page -> different, correct value. This is what
    # proves replay isn't just replaying back the frozen discovery value.
    replayed_value = extract_by_label(PAGE_TEXT_DIFFERENT_MEMBER, label)
    assert replayed_value == "918.20"


def test_extract_by_label_returns_none_when_label_missing():
    assert extract_by_label(PAGE_TEXT, "Nonexistent Label") is None
