#!/usr/bin/env python3
"""
MERIDIAN CORE smoke test -- real browser, not requests.

Answers the last three questions before we touch the core:

  1. Does the hidden _token ride along automatically when Playwright clicks
     a real submit button? (If yes, no schema primitive needed.)
  2. What does a SUCCESSFUL transfer review->post chain look like, and what
     is the confirmation number's shape?
  3. How does Playwright's inner_text() render the shares grid, and what
     locator ladders does OUR OWN digest.py build against this app?

Run from the repo root with the venv active:

    python3 smoke.py                 # headless
    python3 smoke.py --headed        # watch it

Writes recon_out/smoke.txt -- paste that back.

MUTATES: posts one $1.00 transfer on member 100987 (share S0001-3 -> S0070,
both open, small balances). Nothing else.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.discovery.digest import build_observation

BASE = "https://web-sample.interface-hiring.com"
OUT = Path("recon_out")

lines: list[str] = []


def say(*parts):
    msg = " ".join(str(p) for p in parts)
    lines.append(msg)
    print(msg)


def dump_text(page, label):
    say("\n" + "=" * 78)
    say(f"INNER_TEXT :: {label}")
    say(f"URL        : {page.url}")
    say("-" * 78)
    body = page.locator("body").inner_text()
    # repr() per line so we can SEE the tabs -- this is the whole point:
    # extract.py keys off "Label\tValue", so we need to know what actually
    # separates the columns in the shares grid.
    for ln in body.splitlines():
        if ln.strip():
            say("   " + repr(ln))


def dump_digest(page, label):
    """What our own perception layer sees -- including locator ladders."""
    say("\n" + "-" * 78)
    say(f"DIGEST (our digest.py) :: {label}")
    try:
        obs = build_observation(page)
    except Exception as e:
        say(f"   !! build_observation failed: {e}")
        return
    for ref, el in obs.elements.items():
        cands = ", ".join(f"{c.strategy}={c.value!r}" for c in el.candidates)
        say(f"   {ref}: {el.role} {el.name!r}")
        say(f"        ladder: [{cands}]")
        if el.target_url:
            say(f"        target_url: {el.target_url}")


def sign_on(page, operator="teller1", password="password", branch="MAIN-001"):
    page.goto(BASE + "/signon")
    page.fill("input[name='operator']", operator)
    page.fill("input[name='password']", password)
    page.select_option("select[name='branch']", branch)
    page.click("input[type='submit'], button[type='submit']")
    page.wait_for_load_state()
    say(f"\n>>> signed on as {operator} -> {page.url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--member", default="100234")
    ap.add_argument("--xfer-member", default="100987")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    M, XM = args.member, args.xfer_member

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()

        # ---------- 1. member record: the shares grid ------------------
        say("\n\n########## 1. MEMBER RECORD / SHARES GRID ##########")
        sign_on(page)
        page.goto(f"{BASE}/members/{M}")
        dump_text(page, "member record")
        dump_digest(page, "member record")

        # ---------- 2. the token question ------------------------------
        say("\n\n########## 2. DOES _token RIDE ALONG? ##########")
        page.goto(f"{BASE}/{'members'}/{XM}/transfer")
        tok = page.locator("input[name='_token']").get_attribute("value")
        say(f"\n   token rendered on the transfer form : {tok!r}")
        say("   NOTE: we never read, store, or re-inject this. We fill the")
        say("   visible fields and click the real submit button. If the review")
        say("   page below renders instead of a token error, the browser")
        say("   serialized the hidden field for us and no schema change is needed.")

        dump_digest(page, "transfer form")

        # S0001 is on HOLD for this member; S0001-3 -> S0070 are both open.
        page.select_option("select[name='from']", f"{XM}-S0001-3")
        page.select_option("select[name='to']", f"{XM}-S0070")
        page.fill("input[name='amount']", "1.00")
        page.fill("input[name='memo']", "smoke test")
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_load_state()

        dump_text(page, "transfer REVIEW (token test)")
        dump_digest(page, "transfer review")

        review_token = page.locator("input[name='_token']")
        say(f"\n   token on review page: "
            f"{review_token.get_attribute('value') if review_token.count() else '(none)'}")

        # ---------- 3. the post step + confirmation --------------------
        say("\n\n########## 3. POST + CONFIRMATION ##########")
        if page.locator("input[type='submit'], button[type='submit']").count():
            page.click("input[type='submit'], button[type='submit']")
            page.wait_for_load_state()
            dump_text(page, "transfer CONFIRMATION")
        else:
            say("   !! no submit on review page -- check the review dump above")

        # ---------- 4. the exceptional pages, as the browser sees them --
        say("\n\n########## 4. EXCEPTIONAL PAGES ##########")
        for kind in ("maintenance", "server", "validation", "notfound", "permission"):
            page.goto(f"{BASE}/members/{M}?inject={kind}")
            dump_text(page, f"inject={kind}")
            dump_digest(page, f"inject={kind} (recovery controls?)")

        # timeout last -- it kills the session
        page.goto(f"{BASE}/members/{M}?inject=timeout")
        dump_text(page, "inject=timeout")
        dump_digest(page, "inject=timeout (recovery controls?)")

        say("\n\n########## 5. POST-TIMEOUT STATE ##########")
        page.goto(f"{BASE}/members/{M}")
        dump_text(page, "after timeout, plain member fetch")

        browser.close()

    (OUT / "smoke.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n\nWrote {OUT/'smoke.txt'}")


if __name__ == "__main__":
    main()
