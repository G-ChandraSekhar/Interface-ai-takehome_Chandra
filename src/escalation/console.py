"""
Operator console.

Deliberately a signaling plane, not a remote desktop (per the brief's
Section 3.6 scope note): the human takes control by clicking a button here,
then drives the SAME real headed browser window directly with their own
mouse and keyboard -- this console never proxies clicks or renders the
page. Its handlers only flip the shared ControlLease's state; all
Playwright-touching work happens on the run's own thread inside
HandoffController.wait_for_handback(), never here.

Bound to loopback only (127.0.0.1) and has no authentication -- both are
documented limitations for this submission's scope (see REPORT.md's Safety
section), not oversights: this console is meant to run on the same machine
as the automation, for the person already operating it, for one local demo
session at a time.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from src.escalation.lease import IllegalLeaseTransition, LeaseState


def build_app(controller):
    app = FastAPI(title="Operator Console")

    @app.get("/", response_class=HTMLResponse)
    def index():
        state = controller.lease.state
        iv = controller.current_intervention
        if iv:
            iv_html = (
                "<table border=1 cellpadding=6>"
                "<tr><td>Run</td><td>" + iv.run_id + " (" + iv.run_kind + ")</td></tr>"
                "<tr><td>Goal/Capability</td><td>" + iv.goal_or_capability + "</td></tr>"
                "<tr><td>Step</td><td>" + str(iv.step_id) + "</td></tr>"
                "<tr><td>Reason</td><td>" + iv.reason + "</td></tr>"
                "<tr><td>Page URL</td><td>" + iv.page_url + "</td></tr>"
                "</table>"
            )
        else:
            iv_html = "<p>No active intervention.</p>"

        if state == LeaseState.PAUSED:
            action_html = (
                '<button onclick="act(\'/take-control\')">Take control</button>'
            )
        elif state == LeaseState.HUMAN_CONTROL:
            action_html = (
                "<p>You have control. Operate the live browser window directly, "
                "then click Hand back when done.</p>"
                '<button onclick="act(\'/hand-back\')">Hand back</button>'
            )
        else:
            action_html = "<p>No action available in state: " + state.value + "</p>"

        script = (
            "<script>"
            "async function act(path) {"
            "  const r = await fetch(path, {method: 'POST'});"
            "  if (!r.ok) { const b = await r.json(); alert(b.detail || 'Action failed'); }"
            "  location.reload();"
            "}"
            "</script>"
        )

        return (
            "<html><head><title>Operator Console</title>" + script + "</head><body>"
            "<h2>Operator Console</h2>"
            "<p>Lease state: <b>" + state.value + "</b></p>"
            + iv_html
            + action_html
            + "</body></html>"
        )

    @app.post("/take-control")
    def take_control():
        try:
            controller.lease.take_control()
        except IllegalLeaseTransition:
            raise HTTPException(
                status_code=409,
                detail="Already in state '" + controller.lease.state.value + "' -- nothing to take control of.",
            )
        return {"state": controller.lease.state.value}

    @app.post("/hand-back")
    def hand_back():
        try:
            controller.lease.hand_back()
        except IllegalLeaseTransition:
            raise HTTPException(
                status_code=409,
                detail="Already in state '" + controller.lease.state.value + "' -- nothing to hand back.",
            )
        return {"state": controller.lease.state.value}

    @app.get("/status")
    def status():
        iv = controller.current_intervention
        result = {"state": controller.lease.state.value, "intervention": None}
        if iv:
            result["intervention"] = iv.to_dict()
        return result

    return app
