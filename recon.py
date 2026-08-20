#!/usr/bin/env python3
"""
MERIDIAN CORE reconnaissance.

Maps the target's surface without a browser: logs in with requests.Session,
crawls the authenticated pages, and dumps every form (including hidden
fields) so we can see the per-transaction token mechanism before designing
anything.

Run from the repo root with the venv active:

    python3 recon.py                    # teller1
    python3 recon.py --user super1      # supervisor surface

Writes:
    recon_out/digest.txt      <- paste this back
    recon_out/pages/*.html    <- raw HTML, kept for reference
"""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://web-sample.interface-hiring.com"
OUT = Path("recon_out")
PAGES = OUT / "pages"

# Never follow these -- signing off mid-crawl would kill the session, and
# posting a transaction during recon would mutate seed data.
SKIP_PAT = re.compile(r"signoff|sign-off|logout|post|confirm|submit", re.I)

INJECT_KINDS = ["validation", "notfound", "permission", "timeout", "maintenance", "server"]


class PageParser(HTMLParser):
    """Pulls forms (with all inputs), links, and visible text out of a page."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self.links = []
        self.text_parts = []
        self._form = None
        self._select = None
        self._skip_depth = 0
        self._title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._title = True
        elif tag == "form":
            self._form = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "get").lower(),
                "fields": [],
            }
        elif tag in ("input", "textarea") and self._form is not None:
            self._form["fields"].append(
                {
                    "tag": tag,
                    "type": a.get("type", "text"),
                    "name": a.get("name", ""),
                    "value": a.get("value", ""),
                    "id": a.get("id", ""),
                }
            )
        elif tag == "select" and self._form is not None:
            self._select = {
                "tag": "select",
                "type": "select",
                "name": a.get("name", ""),
                "value": "",
                "id": a.get("id", ""),
                "options": [],
            }
        elif tag == "option" and self._select is not None:
            self._select["options"].append(a.get("value", ""))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._title = False
        elif tag == "select" and self._select is not None and self._form is not None:
            self._form["fields"].append(self._select)
            self._select = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._title:
            self.title += data.strip()
        elif self._skip_depth == 0:
            s = data.strip()
            if s:
                self.text_parts.append(s)

    @property
    def text(self):
        return "\n".join(self.text_parts)


def parse(html):
    p = PageParser()
    p.feed(html)
    return p


def slug(url):
    path = urlparse(url).path.strip("/") or "root"
    q = urlparse(url).query
    name = re.sub(r"[^A-Za-z0-9]+", "_", path + ("_" + q if q else ""))
    return name[:80]


def describe(url, resp, page, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"URL    : {url}")
    lines.append(f"STATUS : {resp.status_code}")
    if resp.url != url:
        lines.append(f"FINAL  : {resp.url}   (redirected)")
    lines.append(f"TITLE  : {page.title}")

    for i, f in enumerate(page.forms):
        lines.append(f"  FORM[{i}] {f['method'].upper()} action={f['action']!r}")
        for fl in f["fields"]:
            bits = f"    - {fl['type']:<9} name={fl['name']!r}"
            if fl.get("id"):
                bits += f" id={fl['id']!r}"
            if fl["type"] == "hidden":
                bits += f"  <<< HIDDEN value={fl['value']!r}"
            elif fl.get("value"):
                bits += f" value={fl['value']!r}"
            if fl.get("options"):
                bits += f" options={fl['options'][:8]}"
            lines.append(bits)

    if page.links:
        lines.append(f"  LINKS  : {sorted(set(page.links))}")

    body = page.text
    lines.append("  TEXT   :")
    for ln in body.splitlines()[:45]:
        lines.append("    | " + ln)
    if len(body.splitlines()) > 45:
        lines.append(f"    | ... ({len(body.splitlines()) - 45} more lines)")


def fetch(sess, url, lines, save=True):
    try:
        r = sess.get(url, timeout=20, allow_redirects=True)
    except Exception as e:
        lines.append(f"\n!! GET {url} failed: {e}")
        return None, None
    p = parse(r.text)
    if save:
        (PAGES / f"{slug(url)}.html").write_text(r.text, encoding="utf-8")
    describe(url, r, p, lines)
    return r, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="teller1")
    ap.add_argument("--password", default="password")
    ap.add_argument("--member", default="100234")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--max-pages", type=int, default=45)
    args = ap.parse_args()

    PAGES.mkdir(parents=True, exist_ok=True)
    lines = [f"MERIDIAN CORE recon -- operator={args.user}"]
    sess = requests.Session()

    # ---- 1. landing + sign-on form -------------------------------------
    lines.append("\n\n########## 1. LANDING / SIGN-ON ##########")
    r, p = fetch(sess, BASE + "/", lines)
    if p is None:
        print("\n".join(lines))
        return

    login_form = next(
        (f for f in p.forms if any(x["type"] == "password" for x in f["fields"])), None
    )
    if login_form is None:
        for probe in ("/signon", "/login", "/menu"):
            r, p = fetch(sess, BASE + probe, lines)
            if p:
                login_form = next(
                    (f for f in p.forms if any(x["type"] == "password" for x in f["fields"])),
                    None,
                )
                if login_form:
                    break

    # ---- 2. sign on ----------------------------------------------------
    lines.append("\n\n########## 2. SIGN ON ##########")
    if login_form is None:
        lines.append("!! No password form found -- inspect section 1 manually.")
    else:
        payload = {}
        for fl in login_form["fields"]:
            if not fl["name"]:
                continue
            if fl["type"] == "password":
                payload[fl["name"]] = args.password
            elif fl["type"] == "hidden":
                payload[fl["name"]] = fl["value"]
            elif fl["type"] == "select":
                opts = [o for o in fl.get("options", []) if o]
                payload[fl["name"]] = opts[0] if opts else ""
            elif fl["type"] in ("submit", "button"):
                continue
            else:
                # first non-password text field is the operator id
                payload[fl["name"]] = args.user if "pass" not in fl["name"].lower() else ""
        lines.append(f"  POST {login_form['action']!r} payload keys={list(payload)}")
        lines.append(f"  (operator field guessed -- verify against FORM[] dump above)")
        try:
            action = urljoin(r.url, login_form["action"] or r.url)
            pr = sess.post(action, data=payload, timeout=20, allow_redirects=True)
            pp = parse(pr.text)
            (PAGES / "post_signon.html").write_text(pr.text, encoding="utf-8")
            describe(action + "  [POST]", pr, pp, lines)
        except Exception as e:
            lines.append(f"  !! sign-on POST failed: {e}")

    # ---- 3. crawl the authenticated surface ----------------------------
    lines.append("\n\n########## 3. AUTHENTICATED CRAWL ##########")
    seen = set()
    queue = [(BASE + "/menu", 0)]
    while queue and len(seen) < args.max_pages:
        url, depth = queue.pop(0)
        url = url.split("#")[0]
        if url in seen or depth > args.depth:
            continue
        if urlparse(url).netloc != urlparse(BASE).netloc:
            continue
        if SKIP_PAT.search(url):
            lines.append(f"\n  (skipped, mutating/signoff: {url})")
            continue
        seen.add(url)
        _, pg = fetch(sess, url, lines)
        if pg:
            for href in pg.links:
                nxt = urljoin(url, href)
                if nxt not in seen:
                    queue.append((nxt, depth + 1))

    # ---- 4. member inquiry + the transaction forms ----------------------
    lines.append("\n\n########## 4. MEMBER + TRANSACTION FORMS ##########")
    for path in (
        f"/members/{args.member}",
        f"/members/{args.member}/transfer",
        f"/members/{args.member}/shares/new",
        f"/members/{args.member}/update",
        f"/members/{args.member}/hold",
    ):
        fetch(sess, BASE + path, lines)

    # ---- 5. error injection --------------------------------------------
    lines.append("\n\n########## 5. INJECTED ERROR STATES ##########")
    for kind in INJECT_KINDS:
        u = f"{BASE}/members/{args.member}?inject={kind}"
        try:
            rr = sess.get(u, timeout=20, allow_redirects=False)
            pp = parse(rr.text)
            first = pp.text.splitlines()[:6]
            lines.append(f"\n  inject={kind:<12} HTTP {rr.status_code}")
            lines.append(f"    location: {rr.headers.get('Location', '-')}")
            for ln in first:
                lines.append("    | " + ln)
        except Exception as e:
            lines.append(f"  inject={kind}: FAILED {e}")

    OUT.mkdir(exist_ok=True)
    (OUT / "digest.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n\nWrote {OUT/'digest.txt'} and {len(list(PAGES.glob('*.html')))} pages.")


if __name__ == "__main__":
    main()
