"""
Checkpoint matching.

A checkpoint's url_pattern (e.g. "/desk/member/{member_id}") is rendered
against THIS replay invocation's actual parameter values and compared to
the path replay actually ended on. Pure string logic -- no Playwright
dependency -- so it's directly unit-testable.
"""

from __future__ import annotations

from urllib.parse import urlparse


def render_url_pattern(pattern, params):
    try:
        return pattern.format(**params)
    except KeyError as e:
        raise ValueError("Checkpoint pattern references unknown param: " + str(e)) from e


def checkpoint_met(pattern, params, actual_url):
    expected_path = render_url_pattern(pattern, params)
    actual_path = urlparse(actual_url).path
    return expected_path == actual_path
