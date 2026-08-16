"""
Discovery tools.

These are the only ways the LLM can affect the world. Every action-taking
tool goes through PolicyEngine.check_action() before it touches the page --
this is what makes the allowlist in config/allowlist.yaml actually binding
rather than aspirational. `navigate`, `click`, `type`, and `select` are the
only ones that can mutate page state; `read_text` and `screenshot` are
observational only.

`mark_output` and `finish` aren't page actions at all -- they're how the
model tells us it believes the goal is satisfied. Per the brief's discovery
completion contract, the run only ends when every declared required output
has been visibly marked, not when the model simply stops talking.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.discovery.digest import Observation
from src.guardrails.engine import PolicyEngine
from src.guardrails.result import PolicyDecision

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to an absolute URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": (
                "Click the element with the given reference from the most recent observation. "
                "Refs look like 'e3' and come only from the observation you were shown -- never "
                "invent a ref."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type text into the textbox with the given reference, replacing its current value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["ref", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "Choose an option in the dropdown with the given reference, by visible option text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "option_text": {"type": "string"},
                },
                "required": ["ref", "option_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": "Read the visible text content of the element with the given reference.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for",
            "description": "Wait briefly (up to a few seconds) for the page to settle, e.g. after a navigation or slow load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "description": "How long to wait, max 5."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_output",
            "description": (
                "Declare that you have found the value of one of the goal's required outputs. "
                "Call this as soon as you can see the value on the page -- do not wait until "
                "the very end of the run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call this once every required output has been marked and the goal is fully "
                "complete. This ends the run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
    },
]


@dataclass
class ToolResult:
    ok: bool
    message: str
    is_finish: bool = False
    is_mark_output: bool = False
    output_name: str | None = None
    output_value: str | None = None


def execute_tool(
    name: str,
    args: dict,
    *,
    page,
    observation: Observation,
    policy: PolicyEngine,
    mutate_confirmed: bool = False,
    irreversible_confirmed: bool = False,
) -> ToolResult:
    if name == "navigate":
        url = args["url"]
        check = policy.check_origin(url)
        if not check.allowed:
            return ToolResult(ok=False, message=f"DENIED by policy: {check.reason}")
        page.goto(url)
        return ToolResult(ok=True, message=f"Navigated to {url}")

    if name in ("click", "type", "select", "read_text"):
        ref = args.get("ref")
        el = observation.elements.get(ref)
        if el is None:
            return ToolResult(
                ok=False,
                message=f"Unknown ref '{ref}'. It must come from the most recent observation.",
            )

        action_type = name
        # For click, check against the element's destination (target_url)
        # when known -- a link/submit button must be classified by where it
        # navigates to, not by the (possibly safe) page it's clicked from.
        # Other actions (type, select, read_text) don't navigate, so the
        # current page URL is the right thing to check.
        check_url = el.target_url if (name == "click" and el.target_url) else page.url
        check = policy.check_action(
            action_type,
            check_url,
            confirmed=mutate_confirmed or irreversible_confirmed,
            artifact_approved=False,
        )
        if check.decision == PolicyDecision.DENY:
            return ToolResult(ok=False, message=f"DENIED by policy: {check.reason}")
        if check.decision == PolicyDecision.REQUIRE_CONFIRMATION:
            return ToolResult(
                ok=False,
                message=(
                    f"BLOCKED: {check.reason} This run was not started with the required "
                    f"confirmation flag for a {check.risk_tier.value} action."
                ),
            )

        loc = el.playwright_locator
        if name == "click":
            loc.click()
            return ToolResult(ok=True, message=f"Clicked {ref} ('{el.name}')")
        if name == "type":
            loc.fill(args["text"])
            return ToolResult(ok=True, message=f"Typed into {ref} ('{el.name}')")
        if name == "select":
            loc.select_option(label=args["option_text"])
            return ToolResult(ok=True, message=f"Selected '{args['option_text']}' in {ref}")
        if name == "read_text":
            text = loc.inner_text()
            return ToolResult(ok=True, message=f"{ref} text: {text!r}")

    if name == "wait_for":
        seconds = min(float(args.get("seconds", 1.0)), 5.0)
        page.wait_for_timeout(seconds * 1000)
        return ToolResult(ok=True, message=f"Waited {seconds}s")

    if name == "mark_output":
        return ToolResult(
            ok=True,
            message=f"Marked output {args['name']} = {args['value']!r}",
            is_mark_output=True,
            output_name=args["name"],
            output_value=args["value"],
        )

    if name == "finish":
        return ToolResult(ok=True, message=args.get("summary", "Done."), is_finish=True)

    return ToolResult(ok=False, message=f"Unknown tool '{name}'")
