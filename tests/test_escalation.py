from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.escalation.console import build_app
from src.escalation.controller import HandoffController
from src.escalation.intervention import Intervention
from src.escalation.lease import ControlLease, IllegalLeaseTransition, LeaseState


def test_lease_starts_agent_running():
    lease = ControlLease()
    assert lease.state == LeaseState.AGENT_RUNNING
    assert lease.agent_may_act()


def test_lease_full_happy_path():
    lease = ControlLease()
    lease.pause()
    assert lease.state == LeaseState.PAUSED
    assert not lease.agent_may_act()
    lease.take_control()
    assert lease.state == LeaseState.HUMAN_CONTROL
    lease.hand_back()
    assert lease.state == LeaseState.RESUMING
    lease.resume_complete()
    assert lease.state == LeaseState.AGENT_RUNNING
    assert lease.agent_may_act()


def test_lease_rejects_illegal_transition():
    lease = ControlLease()
    with pytest.raises(IllegalLeaseTransition):
        lease.take_control()


def test_lease_rejects_skipping_human_control():
    lease = ControlLease()
    lease.pause()
    with pytest.raises(IllegalLeaseTransition):
        lease.hand_back()


def test_lease_terminate_from_any_state():
    lease = ControlLease()
    lease.pause()
    lease.terminate()
    assert lease.state == LeaseState.TERMINAL
    with pytest.raises(IllegalLeaseTransition):
        lease.pause()


def test_intervention_to_dict_has_all_fields():
    iv = Intervention(
        run_id="run1",
        run_kind="discovery",
        goal_or_capability="look up member",
        step_id="s2",
        reason="model stuck",
        page_url="http://localhost/desk",
    )
    d = iv.to_dict()
    assert d["run_id"] == "run1"
    assert d["run_kind"] == "discovery"
    assert d["step_id"] == "s2"
    assert d["reason"] == "model stuck"
    assert "created_at" in d


class _FakeEvidence:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, **fields):
        self.events.append((event_type, fields))


def test_console_index_shows_no_intervention_initially():
    controller = HandoffController(_FakeEvidence())
    client = TestClient(build_app(controller))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No active intervention" in resp.text


def test_console_status_reflects_lease_state():
    controller = HandoffController(_FakeEvidence())
    client = TestClient(build_app(controller))
    assert client.get("/status").json()["state"] == "agent_running"
    controller.lease.pause()
    assert client.get("/status").json()["state"] == "paused"


def test_console_take_control_and_hand_back_transition_lease():
    controller = HandoffController(_FakeEvidence())
    controller.lease.pause()
    client = TestClient(build_app(controller))

    resp = client.post("/take-control")
    assert resp.json()["state"] == "human_control"
    assert controller.lease.state == LeaseState.HUMAN_CONTROL

    resp = client.post("/hand-back")
    assert resp.json()["state"] == "resuming"
    assert controller.lease.state == LeaseState.RESUMING


def test_console_shows_intervention_details():
    controller = HandoffController(_FakeEvidence())
    controller.request_intervention(
        run_id="run1",
        run_kind="discovery",
        goal_or_capability="look up member 9999",
        step_id=None,
        reason="model stuck, no viable next action",
        page_url="http://localhost/desk/search",
    )
    client = TestClient(build_app(controller))
    resp = client.get("/")
    assert "run1" in resp.text
    assert "model stuck" in resp.text
    assert "Take control" in resp.text


def test_double_take_control_returns_clean_409_not_a_crash():
    """Regression test: a browser refresh resubmitting the last POST (a
    very ordinary thing for an operator to do by accident) used to crash
    this endpoint with an unhandled IllegalLeaseTransition. It must now
    return a clean 409 with an actionable message instead."""
    controller = HandoffController(_FakeEvidence())
    controller.lease.pause()
    client = TestClient(build_app(controller))

    first = client.post("/take-control")
    assert first.status_code == 200
    assert first.json()["state"] == "human_control"

    second = client.post("/take-control")
    assert second.status_code == 409
    assert "human_control" in second.json()["detail"]
    assert controller.lease.state == LeaseState.HUMAN_CONTROL


def test_hand_back_without_control_returns_clean_409_not_a_crash():
    controller = HandoffController(_FakeEvidence())
    client = TestClient(build_app(controller))

    resp = client.post("/hand-back")
    assert resp.status_code == 409
    assert controller.lease.state == LeaseState.AGENT_RUNNING


class _FakeMainFrame:
    def __init__(self, url):
        self.url = url


class _FakePage:
    def __init__(self):
        self.main_frame = _FakeMainFrame("http://localhost/desk")
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    def remove_listener(self, event, handler):
        self._handlers.pop(event, None)

    def simulate_navigation(self, url):
        self.main_frame = _FakeMainFrame(url)
        handler = self._handlers.get("framenavigated")
        if handler:
            handler(self.main_frame)


def test_wait_for_handback_blocks_until_operator_acts_then_returns_recorded_actions():
    evidence = _FakeEvidence()
    page = _FakePage()
    controller = HandoffController(evidence, page=page)
    controller.request_intervention(
        run_id="run1",
        run_kind="discovery",
        goal_or_capability="test goal",
        step_id=None,
        reason="stuck",
        page_url="http://localhost/desk",
    )

    result_holder = {}

    def run_wait():
        result_holder["actions"] = controller.wait_for_handback(poll_interval=0.05, timeout=5)

    waiter = threading.Thread(target=run_wait)
    waiter.start()

    time.sleep(0.1)
    assert waiter.is_alive()

    controller.lease.take_control()
    time.sleep(0.1)
    page.simulate_navigation("http://localhost/desk/member/9999")
    controller.lease.hand_back()

    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert controller.lease.state == LeaseState.AGENT_RUNNING

    actions = result_holder["actions"]
    assert len(actions) == 1
    assert actions[0]["url"] == "http://localhost/desk/member/9999"

    events = [e for e, _ in evidence.events]
    assert "intervention_created" in events
    assert "operator_took_control" in events
    assert "operator_handed_back" in events


def test_wait_for_handback_times_out_if_operator_never_acts():
    evidence = _FakeEvidence()
    controller = HandoffController(evidence, page=_FakePage())
    controller.request_intervention(
        run_id="run1",
        run_kind="discovery",
        goal_or_capability="test goal",
        step_id=None,
        reason="stuck",
        page_url="http://localhost/desk",
    )
    with pytest.raises(TimeoutError):
        controller.wait_for_handback(poll_interval=0.05, timeout=0.3)
