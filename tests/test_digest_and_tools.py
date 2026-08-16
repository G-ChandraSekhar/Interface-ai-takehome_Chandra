from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from src.discovery.digest import build_observation
from src.discovery.tools import execute_tool
from src.guardrails.engine import PolicyEngine


@pytest.fixture
def browser_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


@pytest.fixture
def policy():
    return PolicyEngine()


def _login(page, base_url, policy):
    page.goto(f"{base_url}/desk/login")
    obs = build_observation(page)
    username_ref = next(r for r, e in obs.elements.items() if e.name.lower() == "username")
    password_ref = next(r for r, e in obs.elements.items() if e.name.lower() == "password")
    login_ref = next(
        r for r, e in obs.elements.items() if e.role == "button" and "sign in" in e.name.lower()
    )
    execute_tool("type", {"ref": username_ref, "text": "teller1"}, page=page, observation=obs, policy=policy)
    execute_tool("type", {"ref": password_ref, "text": "training-only"}, page=page, observation=obs, policy=policy)
    execute_tool("click", {"ref": login_ref}, page=page, observation=obs, policy=policy)


def test_digest_finds_login_fields(mock_app_a, browser_page, policy):
    browser_page.goto(f"{mock_app_a}/desk/login")
    obs = build_observation(browser_page)
    roles_names = {(e.role, e.name.lower()) for e in obs.elements.values()}
    assert ("textbox", "username") in roles_names
    assert ("textbox", "password") in roles_names
    assert any(e.role == "button" and "sign in" in e.name.lower() for e in obs.elements.values())


def test_full_lookup_flow_reaches_member_detail(mock_app_a, browser_page, policy):
    _login(browser_page, mock_app_a, policy)
    obs = build_observation(browser_page)
    member_id_ref = next(r for r, e in obs.elements.items() if e.role == "textbox")
    search_ref = next(
        r for r, e in obs.elements.items() if e.role == "button" and "search" in e.name.lower()
    )
    execute_tool("type", {"ref": member_id_ref, "text": "4521"}, page=browser_page, observation=obs, policy=policy)
    execute_tool("click", {"ref": search_ref}, page=browser_page, observation=obs, policy=policy)

    obs = build_observation(browser_page)
    view_ref = next(r for r, e in obs.elements.items() if "view record" in e.name.lower())
    execute_tool("click", {"ref": view_ref}, page=browser_page, observation=obs, policy=policy)

    assert "/desk/member/4521" in browser_page.url
    body = browser_page.locator("body").inner_text()
    assert "Dana Whitfield" in body
    assert "2,410.55" in body


def test_off_allowlist_navigate_denied(mock_app_a, browser_page, policy):
    browser_page.goto(f"{mock_app_a}/desk/login")
    obs = build_observation(browser_page)
    result = execute_tool(
        "navigate", {"url": "https://evil.example.com"}, page=browser_page, observation=obs, policy=policy
    )
    assert not result.ok
    assert "DENIED" in result.message


def test_mutating_link_click_blocked_without_confirmation(mock_app_a, browser_page, policy):
    _login(browser_page, mock_app_a, policy)
    browser_page.goto(f"{mock_app_a}/desk/member/4521")
    obs = build_observation(browser_page)
    open_ref = next(r for r, e in obs.elements.items() if "open sub-account" in e.name.lower())

    result = execute_tool("click", {"ref": open_ref}, page=browser_page, observation=obs, policy=policy)
    assert not result.ok
    assert "BLOCKED" in result.message
    # page must not have navigated
    assert "subaccount/new" not in browser_page.url


def test_mutating_link_click_allowed_with_confirmation(mock_app_a, browser_page, policy):
    _login(browser_page, mock_app_a, policy)
    browser_page.goto(f"{mock_app_a}/desk/member/4521")
    obs = build_observation(browser_page)
    open_ref = next(r for r, e in obs.elements.items() if "open sub-account" in e.name.lower())

    result = execute_tool(
        "click", {"ref": open_ref}, page=browser_page, observation=obs, policy=policy, mutate_confirmed=True
    )
    assert result.ok
    assert "subaccount/new" in browser_page.url


def test_unknown_ref_rejected(mock_app_a, browser_page, policy):
    browser_page.goto(f"{mock_app_a}/desk/login")
    obs = build_observation(browser_page)
    result = execute_tool("click", {"ref": "e999"}, page=browser_page, observation=obs, policy=policy)
    assert not result.ok
    assert "Unknown ref" in result.message
