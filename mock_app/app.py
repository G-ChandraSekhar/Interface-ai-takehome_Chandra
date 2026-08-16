"""
CorePoint-style mock banking back office.

Deliberately legacy: server-rendered HTML, table-based layout, no test IDs,
no framework classes, sparse semantics -- this is the "hostile surface"
option the assignment brief calls out (Section 4) so the discovery agent and
the replay locator ladder are exercised against something closer to the real
environment than a modern SPA demo.

Run with:
    TENANT=a PORT=4478 python mock_app/app.py
    TENANT=b PORT=4479 python mock_app/app.py
"""

import os
import sys

from flask import Flask, redirect, render_template, request, session, url_for

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_app.chaos import apply_chaos
from mock_app.seed import MEMBERS, RESTRICTED_IDS, SUPERVISOR_CODE
from mock_app.tenants import get_tenant

TENANT_ID = os.environ.get("TENANT", "a")
TENANT = get_tenant(TENANT_ID)
PORT = int(os.environ.get("PORT", "4478" if TENANT_ID == "a" else "4479"))

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"  # local fictional demo only

MOCK_USERNAME = "teller1"
MOCK_PASSWORD = "training-only"


def _chaos_param() -> str:
    return request.args.get("chaos") or session.get("chaos") or "none"


def _base_ctx(**kw):
    return {"tenant": TENANT, **kw}


@app.route(f"{TENANT.route_prefix}/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        session["chaos"] = request.form.get("chaos", "none")
        if (
            request.form.get("username") == MOCK_USERNAME
            and request.form.get("password") == MOCK_PASSWORD
        ):
            session["authenticated"] = True
            return redirect(url_for("desk"))
        error = "Invalid credentials."
    return render_template("login.html", **_base_ctx(error=error))


def _require_auth():
    return session.get("authenticated") is True


@app.route(TENANT.route_prefix)
def desk():
    if not _require_auth():
        return redirect(url_for("login"))
    return render_template("desk.html", **_base_ctx())


@app.route(f"{TENANT.route_prefix}/search")
def search():
    if not _require_auth():
        return redirect(url_for("login"))
    member_id = request.args.get(TENANT.member_id_field_name, "").strip()
    return render_template("search_result.html", **_base_ctx(member_id=member_id))


@app.route(f"{TENANT.route_prefix}/member/<member_id>")
def member_detail(member_id):
    if not _require_auth():
        return redirect(url_for("login"))

    directive = apply_chaos(_chaos_param(), "member_detail")
    if directive == "SHOW_SESSION_EXPIRED":
        session["chaos"] = "none"  # fires once, then clears -- recoverable
        return render_template("session_expired.html", **_base_ctx(member_id=member_id))

    if member_id in RESTRICTED_IDS:
        return render_template("denied.html", **_base_ctx(member_id=member_id)), 403

    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", **_base_ctx(member_id=member_id)), 404

    return render_template("member_detail.html", **_base_ctx(member=member))


@app.route(f"{TENANT.route_prefix}/member/<member_id>/subaccount/new")
def subaccount_new(member_id):
    if not _require_auth():
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", **_base_ctx(member_id=member_id)), 404
    return render_template("subaccount_new.html", **_base_ctx(member=member))


@app.route(f"{TENANT.route_prefix}/member/<member_id>/subaccount/review", methods=["POST"])
def subaccount_review(member_id):
    if not _require_auth():
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", **_base_ctx(member_id=member_id)), 404

    account_type = request.form.get("account_type", "")
    opening_deposit = request.form.get("opening_deposit", "")
    return render_template(
        "subaccount_review.html",
        **_base_ctx(member=member, account_type=account_type, opening_deposit=opening_deposit),
    )


@app.route(f"{TENANT.route_prefix}/member/<member_id>/subaccount/confirm", methods=["POST"])
def subaccount_confirm(member_id):
    if not _require_auth():
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", **_base_ctx(member_id=member_id)), 404

    account_type = request.form.get("account_type", "")
    opening_deposit = request.form.get("opening_deposit", "")

    directive = apply_chaos(_chaos_param(), "subaccount_submit")

    if directive == "RAISE_APP_ERROR":
        return render_template("app_error.html", **_base_ctx(member=member)), 500

    if directive == "REQUIRE_SUPERVISOR_CODE":
        supplied = request.form.get("supervisor_code", "")
        if supplied != SUPERVISOR_CODE:
            return render_template(
                "subaccount_review.html",
                **_base_ctx(
                    member=member,
                    account_type=account_type,
                    opening_deposit=opening_deposit,
                    require_supervisor=True,
                    supervisor_error=bool(supplied),
                ),
            )

    return render_template(
        "subaccount_success.html",
        **_base_ctx(member=member, account_type=account_type, opening_deposit=opening_deposit),
    )


if __name__ == "__main__":
    print(f"Starting tenant '{TENANT_ID}' ({TENANT.display_name}) on port {PORT}")
    print(f"  Login: http://localhost:{PORT}{TENANT.route_prefix}/login")
    app.run(host="127.0.0.1", port=PORT, debug=False)
