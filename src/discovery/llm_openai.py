"""
OpenAI client wrapper for the discovery loop.

Deliberately thin: loop.py owns all control flow (when to stop, how many
steps, what counts as stuck). This module only knows how to turn a message
history + tool schema into one OpenAI API call and hand back a normalized
result, so loop.py isn't coupled to the OpenAI SDK's specific response
shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from openai import OpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_assistant_message: dict = field(default_factory=dict)


class OpenAIDiscoveryClient:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
        self.client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])

    def decide(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            # The loop executes tool calls one at a time and appends exactly
            # one response per call before the next API call. If the model
            # returned several tool_calls in one turn, OpenAI requires a
            # matching tool response for every single one before it will
            # accept the next request -- so we tell it not to batch them.
            parallel_tool_calls=False,
        )
        message = response.choices[0].message

        tool_calls: list[ToolCall] = []
        for tc in (message.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        raw_assistant_message = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            raw_assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ]

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            raw_assistant_message=raw_assistant_message,
        )
