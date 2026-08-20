"""
Handoff controller.

Owns the coordination between a paused run and the operator console. The
console runs in a background thread (uvicorn), but Playwright's sync API is
NOT thread-safe -- a page created on one thread cannot be safely touched
from another. So the console's HTTP handlers only ever flip the lease's
state (thread-safe, protected by a lock) and never touch `page` directly.
All actual Playwright work -- attaching/detaching the human-action
recorder -- happens inside wait_for_handback(), which runs on the SAME
thread that owns the page (the paused discovery loop or replay engine).

Waiting is event-driven, via ControlLease.wait_for_state() -- not polled.
See lease.py's docstring for why: a polling design has to choose an
interval, and any interval leaves a window where a brief state can be
missed entirely; event-driven waiting has no such window.
"""

from __future__ import annotations

import threading
import time

from src.escalation.human_recorder import HumanActionRecorder
from src.escalation.intervention import Intervention
from src.escalation.lease import ControlLease, LeaseState


class HandoffController:
    def __init__(self, evidence, page=None):
        self.lease = ControlLease()
        self.evidence = evidence
        self.page = page
        self.current_intervention = None
        self._recorder = None
        self._server = None
        self._server_thread = None
        self._port = None

    def start_console(self, port=4590):
        """Start the operator console, or return the running one's URL.

        Idempotent deliberately. A single run can escalate more than once --
        a discovery that gets stuck, resumes, and then meets an irreversible
        step calls this twice -- and the second call used to construct a
        second uvicorn server on the same port, which fails to bind and logs
        an error while the first server is still serving perfectly well.
        The operator sees a console that works and a traceback that says it
        does not.
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return "http://127.0.0.1:" + str(self._port)

        import uvicorn

        from src.escalation.console import build_app

        app = build_app(self)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()
        self._port = port
        time.sleep(0.3)
        return "http://127.0.0.1:" + str(port)

    def stop_console(self):
        if self._server:
            self._server.should_exit = True
        if self._server_thread:
            self._server_thread.join(timeout=2)
        self._server = None
        self._server_thread = None
        self._port = None

    def request_intervention(self, run_id, run_kind, goal_or_capability, step_id, reason, page_url, screenshot_path=None):
        self.current_intervention = Intervention(
            run_id=run_id,
            run_kind=run_kind,
            goal_or_capability=goal_or_capability,
            step_id=step_id,
            reason=reason,
            page_url=page_url,
            screenshot_path=screenshot_path,
        )
        # Raise the window this run actually owns. An operator with several
        # headed runs behind them has several Chromium windows sitting on
        # plausible-looking pages, and acting in the wrong one is silent:
        # the action really happens, against a session no run is watching,
        # while the paused run sees nothing and reports its checkpoint
        # unmet. Observed exactly once, which was enough.
        if self.page is not None:
            try:
                self.page.bring_to_front()
            except Exception:
                pass
        self.lease.pause()
        self.evidence.log_event("intervention_created", **self.current_intervention.to_dict())
        return self.current_intervention

    def wait_for_handback(self, timeout=None):
        """Blocks until an operator has taken control and handed back, or
        raises TimeoutError if `timeout` seconds elapse first (None waits
        indefinitely, appropriate for a real human who may take a while).
        Returns the list of recorded human actions."""
        start = time.monotonic()

        def remaining():
            if timeout is None:
                return None
            return max(0.0, timeout - (time.monotonic() - start))

        got_control = self.lease.wait_for_state(LeaseState.HUMAN_CONTROL, timeout=remaining())
        if not got_control:
            raise TimeoutError("Timed out waiting for an operator to take control")

        if self.page is not None:
            self._recorder = HumanActionRecorder(self.page)
            self._recorder.attach()
        self.evidence.log_event("operator_took_control")

        got_resuming = self.lease.wait_for_state(LeaseState.RESUMING, timeout=remaining())
        if not got_resuming:
            if self._recorder is not None:
                self._recorder.detach()
            raise TimeoutError("Timed out waiting for the operator to hand back")

        human_actions = []
        if self._recorder is not None:
            self._recorder.detach()
            human_actions = self._recorder.actions
        self.evidence.log_event("operator_handed_back", human_actions=human_actions)
        self.lease.resume_complete()
        return human_actions
