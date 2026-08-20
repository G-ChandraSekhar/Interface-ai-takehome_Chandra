"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_telemetry(tmp_path, monkeypatch):
    """Keeps the test suite out of the real run-history file.

    replay_artifact() appends a line of durable history on every call, and this
    suite calls it a great many times against FakePage. Without redirection,
    running pytest would quietly fill telemetry/runs.jsonl with hundreds of
    synthetic runs, and `cli.py health` would end up reporting on the test
    suite's behaviour rather than the bank console's -- poisoning the exact
    signal the file exists to carry.

    Redirected by environment variable rather than by argument on purpose: the
    tests then exercise the same default-path resolution that production uses,
    instead of a special branch only tests ever take.

    Autouse because the cost of forgetting it on one new test is a silently
    corrupted history file, which is a much worse failure than a redundant
    fixture on a test that never touches telemetry.
    """
    monkeypatch.setenv("REPLAY_TELEMETRY_PATH", str(tmp_path / "runs.jsonl"))
    yield
    os.environ.pop("REPLAY_TELEMETRY_PATH", None)


@pytest.fixture(scope="session")
def mock_app_a():
    """Starts Tenant A of the mock app on port 4478 for the whole test session."""
    env = dict(os.environ, TENANT="a", PORT="4478")
    proc = subprocess.Popen(
        ["python3", str(REPO_ROOT / "mock_app" / "app.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = "http://127.0.0.1:4478"
    for _ in range(30):
        try:
            requests.get(f"{base_url}/desk/login", timeout=0.5)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("mock app (tenant a) did not start in time")

    yield base_url

    proc.terminate()
    proc.wait(timeout=5)
