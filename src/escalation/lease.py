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

Waiting for a state is backed by a Queue of every transition, in order --
NOT a bare "something changed" Event, and NOT the current state snapshot.
That distinction matters and was caught by a real failing test: if
take_control() and hand_back() fire back-to-back with no gap, the *current*
state can jump straight from PAUSED to RESUMING before a waiter ever
observes HUMAN_CONTROL -- a waiter that only checks "is the state right now
X?" (whether by polling on an interval or by waking on a bare change event
and re-checking current state) can miss an intermediate state entirely,
regardless of how it's woken. A Queue doesn't have this problem: every
transition is pushed in order and stays there until consumed, so a waiter
draining the queue for HUMAN_CONTROL will find it even if RESUMING is
already sitting right behind it -- correct for any timing, not just
"faster than the old bug."
"""

from __future__ import annotations

import queue
import threading
import time
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
        # Every successful transition's resulting state is pushed here, in
        # order. This lease has exactly one consumer in practice (the run
        # thread inside HandoffController, draining it sequentially for
        # HUMAN_CONTROL then RESUMING) -- a single Queue, not per-waiter
        # fan-out, is sufficient and correct for that usage.
        self._transitions = queue.Queue()

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
        self._transitions.put(target)

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

    def wait_for_state(self, target_state, timeout=None):
        """Blocks until target_state has been passed through (not
        necessarily still current), or returns False if `timeout` seconds
        elapse first (None waits indefinitely). Drains the transition
        queue in order, so a target state is found even if later
        transitions already happened before this call started waiting."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            try:
                seen = self._transitions.get(timeout=remaining)
            except queue.Empty:
                return False
            if seen == target_state:
                return True
            # Some other transition -- keep draining; the target may still
            # be later in the queue (or this call may simply not be the one
            # that's supposed to observe it, e.g. a stray earlier state).
