"""Base AI agent for the Factory — uses Claude with structured JSON output."""
from __future__ import annotations
import json
import logging
import os
import re
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("FACTORY_MODEL", "claude-sonnet-4-6")


class FactoryAgent:
    name: str = "base"
    department: str = "general"   # CEO|product|marketing|operations|analytics|hr|tech
    role: str = "agent"           # конкретная роль внутри департамента
    system_prompt: str = "You are an AI assistant."

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.client = anthropic.Anthropic(api_key=api_key)

    def think(self, prompt: str, context: dict | None = None, max_tokens: int = 2048) -> str:
        """Call Claude and return raw text response."""
        system = self.system_prompt
        if context:
            ctx_str = json.dumps(context, ensure_ascii=False, indent=2, default=str)
            system += f"\n\n<context>\n{ctx_str}\n</context>"

        try:
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text if resp.content else ""
        except Exception as e:
            logger.error("[%s/%s] Claude API error: %s", self.department, self.role, e)
            return ""

    def think_json(self, prompt: str, context: dict | None = None, max_tokens: int = 2048) -> dict | list:
        """Call Claude expecting JSON response. Returns parsed dict/list."""
        full_prompt = prompt + "\n\nОтветь ТОЛЬКО валидным JSON без пояснений."
        raw = self.think(full_prompt, context, max_tokens)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict | list:
        raw = raw.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if match:
            raw = match.group(1).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            obj_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
            if obj_match:
                try:
                    return json.loads(obj_match.group(1))
                except Exception:
                    pass
            logger.warning("Could not parse JSON from: %s…", raw[:100])
            return {}
