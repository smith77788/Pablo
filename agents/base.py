"""Base class for all BASIC.FOOD AI agents."""
from __future__ import annotations
import json
import os
from typing import Any
import anthropic

MODEL = "claude-opus-4-7"


class BaseAgent:
    """Shared Claude client, tool-loop logic, and streaming helpers."""

    name: str = "base"
    system_prompt: str = "You are a helpful assistant."

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._tools: list[dict] = []
        self._tool_handlers: dict[str, Any] = {}

    def register_tool(self, schema: dict, handler: Any) -> None:
        self._tools.append(schema)
        self._tool_handlers[schema["name"]] = handler

    def run(self, user_message: str, context: dict | None = None) -> str:
        """Run one agent turn and return the final text response."""
        system = self.system_prompt
        if context:
            system += f"\n\n<context>{json.dumps(context, ensure_ascii=False)}</context>"

        messages: list[dict] = [{"role": "user", "content": user_message}]

        while True:
            with self.client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=system,
                tools=self._tools or anthropic.NOT_GIVEN,
                messages=messages,
            ) as stream:
                response = stream.get_final_message()

            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._call_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                return self._extract_text(response)

    def _call_tool(self, name: str, inputs: dict) -> Any:
        handler = self._tool_handlers.get(name)
        if not handler:
            return {"error": f"Unknown tool: {name}"}
        try:
            return handler(**inputs)
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _extract_text(response: Any) -> str:
        parts = []
        for block in response.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts).strip()
