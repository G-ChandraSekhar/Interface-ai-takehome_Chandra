"""
Handoff controller.

Owns the coordination between a paused run and the operator console. The
console runs in a background thread (uvicorn), but Playwright's sync API is
NOT thread-safe -- a page created on one thread cannot be safely touched
from another. So the console's HTTP handlers only ever flip the lease's
state (thread-safe, protected by a lock) and never touch `page` directly.
All actual Playwright work -- attaching/detaching the human-action
recorder -- happens inside wait_for_handback(), which runs on the SAME
thread that owns the page (the paused discovery loop or replay engine),
polling the lease and reacting to state changes itself.
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

    def start_console(self, port=4590):
        import uvicorn

        from src.escalation.console import build_app

        app = build_app(self)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()
        time.sleep(0.3)
        return "http://127.0.0.1:" + str(port)

    def stop_console(self):
        if self._server:
            self._server.should_exit = True
        if self._server_thread:
            self._server_thread.join(timeout=2)

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
        self.lease.pause()
        self.evidence.log_event("intervention_created", **self.current_intervention.to_dict())
        return self.current_intervention

    def wait_for_handback(self, poll_interval=0.5, timeout=None):
        start = time.time()
        attached = False
        while True:
            state = self.lease.state

            if state == LeaseState.HUMAN_CONTROL and not attached:
                if self.page is not None:
                    self._recorder = HumanActionRecorder(self.page)
                    self._recorder.attach()
                attached = True
                self.evidence.log_event("operator_took_control")

            if state == LeaseState.RESUMING:
                human_actions = []
                if attached and self._recorder is not None:
                    self._recorder.detach()
                    human_actions = self._recorder.actions
                self.evidence.log_event("operator_handed_back", human_actions=human_actions)
                self.lease.resume_complete()
                return human_actions

            if timeout is not None and (time.time() - start) > timeout:
                raise TimeoutError("Timed out waiting for operator hand-back")

            time.sleep(poll_interval)
