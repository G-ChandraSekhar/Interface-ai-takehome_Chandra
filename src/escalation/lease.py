"""
Control lease.

Per the brief's Section 3.6 scope note, the operator console is deliberately
a signaling plane, not a remote desktop -- the human operates the SAME
headed browser window directly with their own mouse/keyboard, and this
lease is what coordinates *who is allowed to be doing that right now*, not
how their clicks get transmitted. Automation checks the lease before every
action; a human's console actions transition it.

States and legal transitions:

  AGENT_RUNNING --(pause)--> PAUSED
  PAUSED --(take_control)--> HUMAN_CONTROL
  HUMAN_CONTROL --(hand_back)--> RESUMING
  RESUMING --(resume_complete)--> AGENT_RUNNING
  (any state) --(terminate)--> TERMINAL

Any other transition raises -- deliberately strict. A caller attempting an
action while the lease says HUMAN_CONTROL is a bug, not something to
silently allow.
"""

from __future__ import annotations

import threading
from enum import Enum


class LeaseState(str, Enum):
    AGENT_RUNNING = "agent_running"
    PAUSED = "paused"
    HUMAN_CONTROL = "human_control"
    RESUMING = "resuming"
    TERMINAL = "terminal"


_LEGAL_TRANSITIONS = {
    LeaseState.AGENT_RUNNING: {LeaseState.PAUSED, LeaseState.TERMINAL},
    LeaseState.PAUSED: {LeaseState.HUMAN_CONTROL, LeaseState.TERMINAL},
    LeaseState.HUMAN_CONTROL: {LeaseState.RESUMING, LeaseState.TERMINAL},
    LeaseState.RESUMING: {LeaseState.AGENT_RUNNING, LeaseState.TERMINAL},
    LeaseState.TERMINAL: set(),
}


class IllegalLeaseTransition(Exception):
    pass


class ControlLease:
    def __init__(self):
        self._state = LeaseState.AGENT_RUNNING
        self._lock = threading.Lock()

    @property
    def state(self):
        with self._lock:
            return self._state

    def agent_may_act(self):
        return self.state == LeaseState.AGENT_RUNNING

    def _transition(self, target):
        with self._lock:
            if target not in _LEGAL_TRANSITIONS[self._state]:
                raise IllegalLeaseTransition(
                    "Cannot transition from " + self._state.value + " to " + target.value
                )
            self._state = target

    def pause(self):
        self._transition(LeaseState.PAUSED)

    def take_control(self):
        self._transition(LeaseState.HUMAN_CONTROL)

    def hand_back(self):
        self._transition(LeaseState.RESUMING)

    def resume_complete(self):
        self._transition(LeaseState.AGENT_RUNNING)

    def terminate(self):
        self._transition(LeaseState.TERMINAL)
