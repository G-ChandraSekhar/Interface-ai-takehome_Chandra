"""
Pure-text label/value extraction.

Playwright's `inner_text()` renders legacy table markup as tab-separated
cells, one line per `<tr>`. Two distinct shapes occur in practice, and this
module handles both because the original target only had the first:

1. **Label/value rows** -- `"Label\\tValue"`, or, on a denser layout,
   SEVERAL pairs on one line: `"Member No.:\\t100234\\tName:\\tLovelace, Ada"`.
   The original implementation partitioned on the FIRST tab, which silently
   returned `"100234\\tName:\\tLovelace, Ada"` as the value of "Member No."
   and made "Name" unreachable entirely. Cells are now walked pairwise, so
   an even-width row yields every pair it actually contains.

2. **Data grids** -- a header row (`"Share ID\\tType\\tBalance\\tStatus"`)
   followed by rows of the same width. There is no label/value pair here at
   all; the question is "the Balance cell of the row whose Share ID is X",
   which is a two-dimensional lookup. `extract_from_grid` does that.

Both directions of rule 1 still share one implementation, which is what
makes output parameterization real: `locate_value` runs during discovery to
work out how the model found a value, `extract_by_label`/`extract_from_grid`
run during replay to re-find it on whatever page replay lands on. A value
captured from one member's page therefore re-resolves against a different
member's page rather than being replayed back frozen.

Labels are matched with the trailing colon normalized away, so an artifact
declaring "Name" and a page rendering "Name:" agree. Existing artifacts
whose labels never had colons are unaffected.

Deliberately pure functions (str in, str out) -- no Playwright dependency --
so all of this is unit-testable without a browser.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.split("\t")]


def _norm(label: str) -> str:
    """Trailing-colon-insensitive label matching."""
    return label.strip().rstrip(":").strip()


def _looks_like_pairs(cells: List[str]) -> bool:
    """Is this line label/value pairs, or a row of a data grid?

    Cell count alone cannot tell them apart -- both of these are four cells:

        Member No.:\tName:\tLovelace, Ada     <- two label/value pairs
        Share ID\tType\tBalance\tStatus       <- a grid header

    The discriminator is the colon. Legacy screens punctuate a field label
    and do not punctuate a column heading, which is a typographic
    convention old enough to be reliable and, more to the point, one a
    human reading the screen uses for exactly the same purpose. A two-cell
    line is taken as a pair regardless: a two-column grid is vanishingly
    rare next to the ubiquitous "Label<tab>Value" row, and reading one as
    the other costs nothing since the extraction rule already declares
    which shape it expects.
    """
    if len(cells) < 2 or len(cells) % 2 != 0:
        return False
    if len(cells) == 2:
        return True
    return all(cells[i].endswith(":") for i in range(0, len(cells), 2))


def _parse_rows(page_text: str) -> List[Tuple[str, str]]:
    """Every (label, value) pair on every label/value line.

    A 2-cell line yields one pair; a 4-cell labelled line yields two. Grid
    rows are left to the grid functions -- guessing pairs out of them would
    invent relationships that aren't in the markup.
    """
    rows: List[Tuple[str, str]] = []
    for line in page_text.splitlines():
        if "\t" not in line:
            continue
        cells = _cells(line)
        if not _looks_like_pairs(cells):
            continue
        for i in range(0, len(cells), 2):
            label, value = cells[i], cells[i + 1]
            if label:
                rows.append((label, value))
    return rows


def _parse_grids(page_text: str) -> List[Tuple[List[str], List[List[str]]]]:
    """Every (header_cells, data_rows) block in the page.

    A grid is a line of >=2 tab-separated cells followed by one or more
    lines of the SAME width. Blocks are cut when the width changes, which is
    what separates the shares table from the label/value block above it.
    """
    grids = []
    # Label/value lines are excluded first, or a run of them would look
    # like a header plus data rows purely by having equal widths -- which
    # is exactly what a confirmation screen ("Confirmation:", "Posted:",
    # "Amount:") is.
    lines = [
        ln
        for ln in page_text.splitlines()
        if "\t" in ln and not _looks_like_pairs(_cells(ln))
    ]

    i = 0
    while i < len(lines):
        header = _cells(lines[i])
        if len(header) < 2:
            i += 1
            continue
        body = []
        j = i + 1
        while j < len(lines) and len(_cells(lines[j])) == len(header):
            body.append(_cells(lines[j]))
            j += 1
        if body:
            grids.append((header, body))
            i = j
        else:
            i += 1
    return grids


# --------------------------------------------------------------------------
# Replay-time extraction
# --------------------------------------------------------------------------

def extract_by_label(page_text: str, label: str) -> Optional[str]:
    """The value paired with `label`, or None."""
    target = _norm(label)
    for row_label, row_value in _parse_rows(page_text):
        if _norm(row_label) == target:
            return row_value
    return None


def extract_from_grid(
    page_text: str,
    key_column: str,
    key_value: str,
    value_column: str,
) -> Optional[str]:
    """The `value_column` cell of the row whose `key_column` cell is `key_value`.

    e.g. the Balance of the row whose Share ID is 100234-S0070.
    """
    key_col, val_col, key = _norm(key_column), _norm(value_column), key_value.strip()

    for header, body in _parse_grids(page_text):
        norm_header = [_norm(h) for h in header]
        if key_col not in norm_header or val_col not in norm_header:
            continue
        ki, vi = norm_header.index(key_col), norm_header.index(val_col)
        for row in body:
            if len(row) > max(ki, vi) and row[ki] == key:
                return row[vi]
    return None


# --------------------------------------------------------------------------
# Discovery-time capture
# --------------------------------------------------------------------------

def find_label_for_value(page_text: str, value: str) -> Optional[str]:
    """The label paired with `value` in a label/value row, or None.

    Kept for callers that only handle rule shape 1; `locate_value` below is
    the fuller answer and is what the distiller uses.
    """
    target = value.strip()
    for label, row_value in _parse_rows(page_text):
        if row_value == target:
            return _norm(label)
    return None


def locate_value(page_text: str, value: str) -> Optional[dict]:
    """How the given value can be re-found on a page of this shape.

    Returns a dict the distiller turns straight into an ExtractionRule:

        {"strategy": "table_row_label", "label": "Name"}

        {"strategy": "table_grid_cell", "label": "Balance",
         "key_column": "Share ID", "key_value": "100234-S0070"}

    or None if the value isn't re-findable, in which case the distiller
    refuses rather than freezing the discovery-time value.

    Label/value rows are checked first: when a value appears in both shapes
    the simpler rule is the more stable one, since it needs no row key.
    """
    target = value.strip()

    label = find_label_for_value(page_text, target)
    if label:
        return {"strategy": "table_row_label", "label": label}

    for header, body in _parse_grids(page_text):
        norm_header = [_norm(h) for h in header]
        for row in body:
            for idx, cell in enumerate(row):
                if cell == target and idx < len(norm_header) and idx != 0:
                    return {
                        "strategy": "table_grid_cell",
                        "label": norm_header[idx],
                        "key_column": norm_header[0],
                        "key_value": row[0],
                    }
    return None


def apply_extraction(page_text: str, rule, params: dict) -> Optional[str]:
    """Run an ExtractionRule against a page. The one entry point replay uses.

    For a grid rule the row key comes from `key_input_ref` (this
    invocation's parameters) when set, else from the frozen `key_literal` --
    the same distinction ArtifactStep already draws between `input_ref` and
    `literal_value`, for the same reason: only caller-supplied values are
    safe to vary per invocation.
    """
    strategy = getattr(rule, "strategy", None)

    if strategy == "table_row_label":
        return extract_by_label(page_text, rule.label)

    if strategy == "table_grid_cell":
        key_ref = getattr(rule, "key_input_ref", None)
        if key_ref:
            if key_ref not in params:
                return None
            key_value = str(params[key_ref])
        else:
            key_value = getattr(rule, "key_literal", None)
            if key_value is None:
                return None
        return extract_from_grid(
            page_text,
            key_column=getattr(rule, "key_column", "") or "",
            key_value=key_value,
            value_column=rule.label,
        )

    return None
