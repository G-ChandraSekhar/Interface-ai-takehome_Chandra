from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]


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
