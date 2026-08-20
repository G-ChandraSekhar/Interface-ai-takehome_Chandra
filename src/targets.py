"""
Target configuration.

The seam between "this codebase" and "the specific legacy console it is
pointed at." Everything that used to be a constant in Python -- the sign-on
URL, which selector holds the operator id, what a session-expired page says
-- is declared in config/targets/<id>.yaml and read through here.

Why this exists at all: authentication was previously a `_mock_login()`
function duplicated verbatim in src/discovery/loop.py and
src/replay/engine.py, with the target's credentials and field names written
into both. That is fine for exactly one target and becomes a rewrite for the
second. There is now one `authenticate()`, driven by config, used by both
paths -- so pointing at a new console is a YAML file rather than an edit to
the discovery loop and the replay engine.

Credentials are read from the environment. Each config declares the variable
name and a default; the defaults are only ever the target's own published
demo operators, so a reviewer can run the demo with no setup while a real
deployment sets the vars and never touches them. Nothing here is written to
evidence -- the redactor already masks password-shaped field names at the
write boundary, and the values never enter an artifact in the first place.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

TARGETS_DIR = Path(__file__).resolve().parents[1] / "config" / "targets"

# Artifacts recorded before targets existed carry tenant "a"/"b"; both are
# the mock app. Keeps every previously-distilled artifact replayable.
_LEGACY_TENANT_TO_TARGET = {"a": "mock", "b": "mock"}


class TargetConfigError(RuntimeError):
    pass


class TargetConfig:
    """One target console, as declared in YAML."""

    def __init__(self, data: dict, path: Path):
        self.path = path
        self.id = data.get("id") or path.stem
        self.display_name = data.get("display_name", self.id)
        self.base_url = data.get("base_url")
        self.route_prefix = data.get("route_prefix")
        self.signon = data.get("signon") or {}
        self.credentials = data.get("credentials") or {}
        self.supervisor_credentials = data.get("supervisor_credentials") or {}
        self.session = data.get("session") or {}
        self.fault_injection = data.get("fault_injection") or {}
        self.detectors = data.get("detectors") or {}

    # -- credentials --------------------------------------------------------

    def _resolve(self, spec: dict, name: str) -> str:
        entry = spec.get(name)
        if entry is None:
            raise TargetConfigError(
                "Target '" + self.id + "' declares no credential '" + name + "'"
            )
        if isinstance(entry, str):
            return entry
        env_name = entry.get("env")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]
        default = entry.get("default")
        if default is None:
            raise TargetConfigError(
                "Credential '" + name + "' for target '" + self.id + "' has no value: "
                "set $" + str(env_name)
            )
        return default

    def credential(self, name: str, supervisor: bool = False) -> str:
        spec = self.supervisor_credentials if supervisor else self.credentials
        if supervisor and not spec:
            raise TargetConfigError(
                "Target '" + self.id + "' declares no supervisor profile"
            )
        return self._resolve(spec, name)

    # -- session ------------------------------------------------------------

    def signed_off_marker(self) -> Optional[str]:
        return self.session.get("signed_off_marker")

    def is_signed_on(self, page_text: str) -> Optional[bool]:
        """True/False, or None when this target declares no session markers."""
        off = self.session.get("signed_off_marker")
        on = self.session.get("signed_on_marker")
        if off and off in page_text:
            return False
        if on:
            return on in page_text
        return None if not off else True

    # -- sign on ------------------------------------------------------------

    def signon_url(self, base_url: Optional[str] = None,
                   route_prefix: Optional[str] = None) -> str:
        base = base_url or self.base_url
        if not base:
            raise TargetConfigError(
                "Target '" + self.id + "' has no base_url and none was supplied"
            )
        path = self.signon.get("path", "/login")
        path = path.replace("{route_prefix}", route_prefix or self.route_prefix or "")
        return base.rstrip("/") + path


def load_target(target_id: str) -> Optional[TargetConfig]:
    """Load a target by id, or by the legacy tenant letter. None if unknown."""
    if not target_id:
        return None
    resolved = _LEGACY_TENANT_TO_TARGET.get(target_id, target_id)
    path = TARGETS_DIR / (resolved + ".yaml")
    if not path.exists():
        return None
    with open(path, "r") as f:
        return TargetConfig(yaml.safe_load(f) or {}, path)


def authenticate(
    page,
    target_id: str,
    *,
    base_url: Optional[str] = None,
    route_prefix: Optional[str] = None,
    supervisor: bool = False,
    chaos: str = "none",
) -> str:
    """Establish a session on `page`. Returns the URL landed on.

    The one authentication path in the codebase. Discovery and replay both
    call this; neither knows what the sign-on form looks like.
    """
    target = load_target(target_id)
    if target is None:
        raise TargetConfigError("No target configuration for '" + str(target_id) + "'")

    page.goto(target.signon_url(base_url, route_prefix))

    for _, field in (target.signon.get("fields") or {}).items():
        value = target.credential(field["credential"], supervisor=supervisor)
        selector, kind = field["selector"], field.get("kind", "fill")
        if kind == "select":
            page.select_option(selector, value)
        else:
            page.fill(selector, value)

    # Only the mock target declares this: it stores a fault mode in the
    # session at login. MERIDIAN injects per-request via ?inject= and needs
    # no login-time plumbing, so the branch simply never fires there.
    chaos_field = target.signon.get("chaos_field")
    if chaos_field and chaos != "none":
        page.evaluate(
            "([name, v]) => { const f = document.querySelector('form'); "
            "const i = document.createElement('input'); "
            "i.type = 'hidden'; i.name = name; i.value = v; f.appendChild(i); }",
            [chaos_field, chaos],
        )

    page.click(target.signon["submit"])
    try:
        page.wait_for_load_state()
    except Exception:
        pass

    expected = target.signon.get("success_url_contains")
    if expected and expected not in page.url:
        raise TargetConfigError(
            "Sign-on to '" + target.id + "' did not reach " + expected
            + " (landed on " + page.url + ")"
        )
    return page.url


def set_fault_injection(
    page,
    target_id: str,
    *,
    base_url: Optional[str] = None,
    kind: str = "none",
    rate: float = 0.0,
) -> bool:
    """Drive the target's own fault-injection controls. False if it has none.

    Uses the host's real settings screen rather than a side channel, so what
    is exercised is genuinely the same code path a fault in production would
    take. Never raises: failing to arm a fault must not fail the run that was
    only trying to demonstrate one.
    """
    target = load_target(target_id)
    if target is None or not target.fault_injection:
        return False

    cfg = target.fault_injection
    if cfg.get("mode") != "settings_form":
        return False

    base = base_url or target.base_url
    try:
        page.goto(base.rstrip("/") + cfg.get("path", "/settings"))
        if cfg.get("forced_field"):
            page.select_option(
                "select[name='" + cfg["forced_field"] + "']",
                "" if kind in (None, "none") else kind,
            )
        if cfg.get("rate_field"):
            page.fill("input[name='" + cfg["rate_field"] + "']", str(rate))
        page.click(cfg.get("submit", "input[type='submit']"))
        page.wait_for_load_state()
        return True
    except Exception:
        return False


def clear_fault_injection(page, target_id: str, *, base_url: Optional[str] = None) -> bool:
    """Put the host back how we found it."""
    return set_fault_injection(page, target_id, base_url=base_url, kind="none", rate=0.0)
