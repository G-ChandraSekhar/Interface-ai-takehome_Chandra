"""
Human action recorder.

Records what the human did during their HUMAN_CONTROL window by listening
for frame navigations on the SAME live page they're driving directly (no
remote-control layer to intercept clicks/keystrokes through -- see the
lease module's docstring). This captures navigation-level granularity
(which pages the human visited, in order) rather than individual
click/keystroke events; that's a deliberate, documented scope cut for this
submission (see REPORT.md's Cuts section), not an oversight -- capturing
DOM-level input events would need injecting a script into every page the
human might navigate to, which is a real but more involved addition.

Must only ever be called from the thread that owns the Playwright `page`
object (Playwright's sync API is not thread-safe) -- see controller.py's
docstring for why this matters.
"""

from __future__ import annotations

from datetime import datetime, timezone


class HumanActionRecorder:
    def __init__(self, page):
        self.page = page
        self.actions = []
        self._handler = None

    def attach(self):
        def on_navigation(frame):
            try:
                if frame == self.page.main_frame:
                    self.actions.append(
                        {
                            "type": "navigation",
                            "url": frame.url,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    )
            except Exception:
                pass

        # Attaching is best-effort instrumentation: a surface that doesn't
        # expose Playwright's event API must not be able to break a handoff.
        # Losing the action recording is a much smaller problem than an
        # operator being unable to take control of a live session.
        try:
            self.page.on("framenavigated", on_navigation)
            self._handler = on_navigation
        except (AttributeError, NotImplementedError):
            self._handler = None

    def detach(self):
        if self._handler is not None:
            try:
                self.page.remove_listener("framenavigated", self._handler)
            except Exception:
                pass
            self._handler = None
