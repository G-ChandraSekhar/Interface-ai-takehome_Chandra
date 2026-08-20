"""
Extraction against MERIDIAN CORE's real markup.

The page text below is verbatim `body.inner_text()` captured from
web-sample.interface-hiring.com, tabs and all. It is checked in as a
fixture because the two shapes it contains are exactly what broke the
original extractor, and a synthetic approximation would not have:

  - "Member No.:\tName:\tLovelace, Ada" puts TWO label/value pairs on one
    line. Partitioning on the first tab returned "100234\tName:\tLovelace,
    Ada" as the value of "Member No." and made "Name" unreachable entirely.
  - The shares table is a data grid, not label/value pairs at all. Reading
    one share's balance is a two-dimensional lookup that the row-label
    strategy cannot express.

The second one mattered more than it looks: `find_label_for_value` returned
None for every value in both shapes, and distill.py refuses to distill an
artifact whose outputs have no extraction rule. Discovery would have
completed, then distillation would have failed -- one wasted LLM run per
attempt, with an error message pointing at the wrong thing.
"""

from __future__ import annotations

import pytest

from src.artifact.extract import (
    apply_extraction,
    extract_by_label,
    extract_from_grid,
    find_label_for_value,
    locate_value,
)
from src.artifact.schema import ExtractionRule

MEMBER_RECORD = (
    "MERIDIAN CORE \xa0\xa0Member Services Platform \xa0 v4.2.1\n"
    "Cornerstone Financial Systems\u2122\n"
    "Main Menu \xa0\u00b7\xa0 Member Inquiry \xa0\u00b7\xa0 System Settings \xa0\u00b7\xa0 Sign Off\n"
    "MEMBER RECORD\n"
    "Member No.:\t100234\tName:\tLovelace, Ada\n"
    "E-mail:\tada.lovelacn@example.com\tPhone:\t555-0109\n"
    "Address:\t19 Analytical Way, Springfield\n"
    "SHARES / BALANCES\n"
    "Share ID\tType\tBalance\tStatus\n"
    "100234-S0001\tRegular Shares\t$1,499.00\tHOLD [HOLD]\n"
    "100234-S0070\tShare Draft (Checking)\t$2,241.55\tOPEN\n"
    "100234-S0001-3\tRegular Shares\t$10.00\tOPEN\n"
    "ACTIONS\n"
    "OPR TELLER1 \xa0|\xa0 BR MAIN-001 \xa0|\xa0 08/20/2026 17:40:40 \xa0|\xa0 SID 4F7AD10F"
)

TRANSFER_CONFIRMATION = (
    "TRANSFER POSTED\n"
    "TRANSACTION COMPLETE\n"
    "Confirmation:\tCN480013\n"
    "Posted:\t08/20/2026 17:40:41\n"
    "Amount:\t$1.00\n"
    "100987-S0001-3:\t$4.00 (new balance)\n"
    "100987-S0070:\t$3.25 (new balance)"
)


# --------------------------------------------------------------------------
# Shape 1: label/value rows, including several pairs on one line
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Member No.", "100234"),
        ("Name", "Lovelace, Ada"),
        ("E-mail", "ada.lovelacn@example.com"),
        ("Phone", "555-0109"),
        ("Address", "19 Analytical Way, Springfield"),
    ],
)
def test_every_pair_on_a_multi_pair_row_is_reachable(label, expected):
    assert extract_by_label(MEMBER_RECORD, label) == expected


def test_the_second_pair_on_a_line_was_the_regression():
    """'Name' and 'Phone' are the ones the original parser could not see."""
    assert extract_by_label(MEMBER_RECORD, "Name") == "Lovelace, Ada"
    assert extract_by_label(MEMBER_RECORD, "Phone") == "555-0109"


def test_first_pair_does_not_swallow_the_rest_of_its_line():
    value = extract_by_label(MEMBER_RECORD, "Member No.")
    assert "\t" not in value
    assert value == "100234"


def test_label_matching_is_trailing_colon_insensitive():
    """The page renders 'Name:'; an artifact may declare either form."""
    assert extract_by_label(MEMBER_RECORD, "Name:") == extract_by_label(
        MEMBER_RECORD, "Name"
    )


# --------------------------------------------------------------------------
# Shape 2: the shares grid
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "share,column,expected",
    [
        ("100234-S0070", "Balance", "$2,241.55"),
        ("100234-S0001", "Balance", "$1,499.00"),
        ("100234-S0001-3", "Balance", "$10.00"),
        ("100234-S0070", "Status", "OPEN"),
        ("100234-S0001", "Type", "Regular Shares"),
    ],
)
def test_grid_lookup_is_row_keyed(share, column, expected):
    assert extract_from_grid(MEMBER_RECORD, "Share ID", share, column) == expected


def test_grid_lookup_of_an_absent_row_returns_none_not_a_wrong_row():
    assert extract_from_grid(MEMBER_RECORD, "Share ID", "999-NOPE", "Balance") is None


def test_grid_does_not_leak_into_the_label_value_block_above_it():
    """'Share ID' is a grid header, never a label whose value is 'Type'."""
    assert extract_by_label(MEMBER_RECORD, "Share ID") is None


# --------------------------------------------------------------------------
# Discovery-time capture -- what distill.py depends on
# --------------------------------------------------------------------------

def test_locate_value_prefers_the_simpler_row_rule():
    assert locate_value(MEMBER_RECORD, "Lovelace, Ada") == {
        "strategy": "table_row_label",
        "label": "Name",
    }


def test_locate_value_falls_through_to_a_grid_rule_with_its_row_key():
    assert locate_value(MEMBER_RECORD, "$2,241.55") == {
        "strategy": "table_grid_cell",
        "label": "Balance",
        "key_column": "Share ID",
        "key_value": "100234-S0070",
    }


def test_a_value_that_cannot_be_relocated_reports_none():
    """distill.py must refuse rather than freeze the discovery-time value."""
    assert locate_value(MEMBER_RECORD, "not on this page") is None


def test_find_label_for_value_now_resolves_what_it_used_to_miss():
    assert find_label_for_value(MEMBER_RECORD, "555-0109") == "Phone"


# --------------------------------------------------------------------------
# Replay-time dispatch, both key bindings
# --------------------------------------------------------------------------

def test_grid_rule_keyed_by_invocation_param_varies_per_call():
    rule = ExtractionRule(
        strategy="table_grid_cell",
        label="Balance",
        key_column="Share ID",
        key_input_ref="share_id",
    )
    assert apply_extraction(MEMBER_RECORD, rule, {"share_id": "100234-S0070"}) == "$2,241.55"
    assert apply_extraction(MEMBER_RECORD, rule, {"share_id": "100234-S0001-3"}) == "$10.00"


def test_grid_rule_missing_its_param_fails_rather_than_guessing_a_row():
    rule = ExtractionRule(
        strategy="table_grid_cell",
        label="Balance",
        key_column="Share ID",
        key_input_ref="share_id",
    )
    assert apply_extraction(MEMBER_RECORD, rule, {}) is None


def test_grid_rule_with_a_frozen_key_needs_no_param():
    rule = ExtractionRule(
        strategy="table_grid_cell",
        label="Status",
        key_column="Share ID",
        key_literal="100234-S0001",
    )
    assert apply_extraction(MEMBER_RECORD, rule, {}) == "HOLD [HOLD]"


def test_extraction_rules_survive_a_json_round_trip():
    """Artifacts are JSON on disk; a rule that can't persist is useless."""
    rule = ExtractionRule(
        strategy="table_grid_cell",
        label="Balance",
        key_column="Share ID",
        key_input_ref="share_id",
    )
    revived = ExtractionRule.model_validate_json(rule.model_dump_json())
    assert revived == rule


# --------------------------------------------------------------------------
# The confirmation page -- the output that actually matters to a caller
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Confirmation", "CN480013"),
        ("Amount", "$1.00"),
        ("Posted", "08/20/2026 17:40:41"),
    ],
)
def test_transfer_confirmation_fields_extract(label, expected):
    assert extract_by_label(TRANSFER_CONFIRMATION, label) == expected


def test_post_transfer_share_balances_are_readable_by_share_id():
    """The confirmation reports new balances as label/value, keyed by share."""
    assert extract_by_label(TRANSFER_CONFIRMATION, "100987-S0070") == "$3.25 (new balance)"
