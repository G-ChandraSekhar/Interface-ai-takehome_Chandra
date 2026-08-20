#!/usr/bin/env python3
"""
MERIDIAN CORE recon, round 2.

Round 1 mapped the surface. This answers the four questions that block
schema design:

  A. Does _token rotate after a POST, or is one read per session enough?
  B. What do the review and post-confirmation pages look like (and does
     the post return a confirmation number we must extract)?
  C. What do the six inject kinds ACTUALLY return, measured with a fresh
     session each time so one probe can't poison the next?
  D. Does the supervisor gate fire on POST, and does super1 clear it?

Reuses the parser from recon.py, so keep both files in the repo root.

    python3 recon2.py

Writes recon_out/digest2.txt -- paste that back.

NOTE: this one MUTATES. It posts a $1.00 transfer and (as super1) a hold.
The app is stateful in memory and resets on redeploy, and the brief says
to hammer on it, so this is fine -- but it is why it's a separate script.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urljoin

import requests

from recon import BASE, OUT, PAGES, describe, parse

INJECT_KINDS = ["validation", "notfound", "permission", "timeout", "maintenance", "server"]


def sign_on(operator, password, branch="MAIN-001"):
    """Fresh session, signed on. Returns (session, landing_response)."""
    s = requests.Session()
    s.get(BASE + "/signon", timeout=20)
    r = s.post(
        BASE + "/signon",
        data={"operator": operator, "password": password, "branch": branch},
        timeout=20,
        allow_redirects=True,
    )
    return s, r


def tokens_in(page):
    """Every hidden _token value on a page."""
    out = []
    for f in page.forms:
        for fl in f["fields"]:
            if fl["name"] == "_token":
                out.append(fl["value"])
    return out


def get(sess, url, lines, label="", save_as=None):
    r = sess.get(url, timeout=20, allow_redirects=True)
    p = parse(r.text)
    if save_as:
        (PAGES / f"{save_as}.html").write_text(r.text, encoding="utf-8")
    describe(url + (f"  [{label}]" if label else ""), r, p, lines)
    return r, p


def submit(sess, current_url, form, lines, overrides=None, label="", save_as=None):
    """POST/GET a parsed form, defaults preserved, overrides applied."""
    payload = {}
    for fl in form["fields"]:
        if not fl["name"] or fl["type"] in ("submit", "button"):
            continue
        if fl["type"] == "select":
            opts = [o for o in fl.get("options", []) if o]
            payload[fl["name"]] = opts[0] if opts else ""
        else:
            payload[fl["name"]] = fl.get("value", "")
    payload.update(overrides or {})

    action = urljoin(current_url, form["action"] or current_url)
    lines.append(f"\n  >>> {form['method'].upper()} {action}")
    lines.append(f"      payload={ {k: v for k, v in payload.items()} }")

    if form["method"] == "post":
        r = sess.post(action, data=payload, timeout=20, allow_redirects=True)
    else:
        r = sess.get(action, params=payload, timeout=20, allow_redirects=True)

    p = parse(r.text)
    if save_as:
        (PAGES / f"{save_as}.html").write_text(r.text, encoding="utf-8")
    describe(action + (f"  [{label}]" if label else ""), r, p, lines)
    return r, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--member", default="100234")
    ap.add_argument("--mutate-member", default="100987",
                    help="member used for the real $1 transfer, kept off the demo member")
    args = ap.parse_args()

    PAGES.mkdir(parents=True, exist_ok=True)
    lines = ["MERIDIAN CORE recon round 2"]
    M = args.member
    MM = args.mutate_member

    # ================= A + B: token rotation, review -> post =============
    lines.append("\n\n########## A/B. TOKEN LIFECYCLE + REVIEW->POST CHAIN ##########")
    s, r = sign_on("teller1", "password")
    lines.append(f"  signed on -> {r.url} ({r.status_code})")

    _, p1 = get(s, f"{BASE}/members/{MM}/transfer", lines, "transfer form", "r2_transfer_form")
    t1 = tokens_in(p1)
    lines.append(f"\n  TOKEN @ transfer form : {t1}")

    _, p_upd = get(s, f"{BASE}/members/{MM}/update", lines, "update form", "r2_update_form")
    lines.append(f"  TOKEN @ update form   : {tokens_in(p_upd)}  (same session, different page)")

    if not p1.forms:
        lines.append("  !! no form on transfer page -- stopping A/B")
    else:
        form = p1.forms[0]
        shares = []
        for fl in form["fields"]:
            if fl["name"] == "from":
                shares = [o for o in fl.get("options", []) if o]
        lines.append(f"  shares available: {shares}")

        if len(shares) >= 2:
            # ---- review step
            _, p2 = submit(
                s, f"{BASE}/members/{MM}/transfer", form, lines,
                overrides={"from": shares[0], "to": shares[1],
                           "amount": "1.00", "memo": "recon probe"},
                label="REVIEW", save_as="r2_transfer_review",
            )
            t2 = tokens_in(p2)
            lines.append(f"\n  TOKEN @ review page   : {t2}")
            lines.append(f"  >>> ROTATED AFTER GET->REVIEW? {'YES' if t2 and t2 != t1 else 'no'}")

            # ---- post step
            if p2.forms:
                _, p3 = submit(
                    s, f"{BASE}/members/{MM}/transfer/review", p2.forms[0], lines,
                    label="POST/CONFIRM", save_as="r2_transfer_posted",
                )
                lines.append(f"\n  TOKEN @ confirmation  : {tokens_in(p3)}")
            else:
                lines.append("  !! review page had no form -- inspect r2_transfer_review.html")

            # ---- does the token change AFTER a completed post?
            _, p4 = get(s, f"{BASE}/members/{MM}/transfer", lines,
                        "transfer form AGAIN", "r2_transfer_form_after")
            t4 = tokens_in(p4)
            lines.append(f"\n  TOKEN @ transfer form after posting : {t4}")
            lines.append(f"  >>> ROTATES PER TRANSACTION? {'YES' if t4 and t4 != t1 else 'NO -- session-scoped'}")

    # ---- open-share, the page round 1 missed
    lines.append("\n\n########## B2. OPEN NEW SHARE FORM ##########")
    get(s, f"{BASE}/members/{M}/open-share", lines, "open share", "r2_open_share")

    # ================= C: injections, isolated =========================
    lines.append("\n\n########## C. INJECTIONS (FRESH SESSION EACH) ##########")
    for kind in INJECT_KINDS:
        si, _ = sign_on("teller1", "password")
        for verb, url in (
            ("GET member", f"{BASE}/members/{M}?inject={kind}"),
            ("GET transfer", f"{BASE}/members/{M}/transfer?inject={kind}"),
        ):
            try:
                rr = si.get(url, timeout=20, allow_redirects=False)
                pp = parse(rr.text)
                body = [ln for ln in pp.text.splitlines()
                        if ln not in ("MERIDIAN CORE", "Member Services Platform   v4.2.1",
                                      "Cornerstone Financial Systems™", "Main Menu", "·",
                                      "Member Inquiry", "System Settings", "Sign Off")]
                lines.append(f"\n  inject={kind:<12} {verb:<13} HTTP {rr.status_code}"
                             f"  loc={rr.headers.get('Location', '-')}")
                for ln in body[:8]:
                    lines.append("      | " + ln)
            except Exception as e:
                lines.append(f"  inject={kind} {verb}: FAILED {e}")

    # ================= D: supervisor gating ============================
    lines.append("\n\n########## D. SUPERVISOR GATING ON PLACE HOLD ##########")

    for who in ("teller1", "super1"):
        lines.append(f"\n  ---------- as {who} ----------")
        sh, _ = sign_on(who, "password")
        _, ph = get(sh, f"{BASE}/members/{M}/hold", lines, f"hold form ({who})",
                    f"r2_hold_form_{who}")
        if not ph.forms:
            lines.append(f"  !! no hold form for {who}")
            continue
        _, pr = submit(sh, f"{BASE}/members/{M}/hold", ph.forms[0], lines,
                       overrides={"reason": "FRAUD", "notes": f"recon probe as {who}"},
                       label=f"HOLD REVIEW ({who})", save_as=f"r2_hold_review_{who}")
        if pr.forms:
            submit(sh, f"{BASE}/members/{M}/hold/review", pr.forms[0], lines,
                   label=f"HOLD POST ({who})", save_as=f"r2_hold_post_{who}")
        else:
            lines.append(f"  (no form on hold review page for {who} -- likely blocked here)")

    # ================= E: natural (non-injected) errors =================
    lines.append("\n\n########## E. NATURAL ERROR STATES ##########")

    sn, _ = sign_on("teller1", "password")
    get(sn, f"{BASE}/members?by=number&q=999999", lines, "member not found", "r2_notfound")
    get(sn, f"{BASE}/members?by=name&q=Lovelace", lines, "search by last name", "r2_by_name")

    _, pov = get(sn, f"{BASE}/members/{M}/transfer", lines, "transfer (overdraw setup)")
    if pov.forms:
        shares = [o for o in pov.forms[0]["fields"][1].get("options", []) if o] \
            if len(pov.forms[0]["fields"]) > 1 else []
        if len(shares) >= 2:
            submit(sn, f"{BASE}/members/{M}/transfer", pov.forms[0], lines,
                   overrides={"from": shares[0], "to": shares[1],
                              "amount": "999999.99", "memo": "overdraw probe"},
                   label="OVERDRAW", save_as="r2_overdraw")

    _, pu = get(sn, f"{BASE}/members/{M}/update", lines, "update (bad email setup)")
    if pu.forms:
        submit(sn, f"{BASE}/members/{M}/update", pu.forms[0], lines,
               overrides={"email": "not-an-email", "phone": "abc"},
               label="INVALID EMAIL/PHONE", save_as="r2_bad_email")

    # ---- bad login
    sb = requests.Session()
    rb = sb.post(BASE + "/signon",
                 data={"operator": "teller1", "password": "wrong", "branch": "MAIN-001"},
                 timeout=20, allow_redirects=True)
    pb = parse(rb.text)
    describe(BASE + "/signon  [BAD PASSWORD]", rb, pb, lines)

    OUT.mkdir(exist_ok=True)
    (OUT / "digest2.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n\nWrote {OUT/'digest2.txt'}")


if __name__ == "__main__":
    main()
