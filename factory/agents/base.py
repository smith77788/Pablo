"""Base AI agent — работает с ANTHROPIC_API_KEY (Lovable/любой хостинг) или claude CLI (локально)."""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
from typing import Any, Union

logger = logging.getLogger(__name__)

CLAUDE_BIN = os.getenv("CLAUDE_CODE_EXECPATH", "claude")
MODEL = os.getenv("FACTORY_MODEL", "claude-sonnet-4-6")
TIMEOUT = int(os.getenv("FACTORY_TIMEOUT", "300"))

# Real key looks like sk-ant-api03-... and is 100+ chars; placeholder/empty = no API calls
def _is_real_api_key(key: str) -> bool:
    return bool(key and key.startswith("sk-ant-api") and len(key) > 50 and key.isascii())

_raw_key = os.getenv("ANTHROPIC_API_KEY", "")
_USE_SDK = _is_real_api_key(_raw_key)
API_AVAILABLE = _USE_SDK  # module-level flag for cycle.py to check

if os.getenv("ANTHROPIC_API_KEY") and not _USE_SDK:
    logger.warning(
        "ANTHROPIC_API_KEY is set but looks like a placeholder — AI agent calls will be skipped. "
        "Set a real key (sk-ant-api03-...) to enable AI department phases."
    )


def _make_sdk_client() -> Any:
    """Создаёт Anthropic SDK клиент если есть API ключ."""
    try:
        import anthropic
        return anthropic.Anthropic(api_key=_raw_key)
    except Exception as e:
        logger.warning("SDK init failed: %s, falling back to CLI", e)
        return None


_sdk_client = _make_sdk_client() if _USE_SDK else None


class FactoryAgent:
    name: str = "base"
    department: str = "general"
    role: str = "agent"
    system_prompt: str = "You are an AI assistant."

    def __init__(self) -> None:
        pass

    def think(self, prompt: str, context: dict | None = None, max_tokens: int = 2048) -> str:
        """Вызывает Claude (SDK или CLI) и возвращает текст ответа."""
        # Fast-fail when no real API key and CLI not available — avoids long timeouts
        if not _sdk_client and not _is_real_api_key(_raw_key):
            logger.debug("[%s/%s] No real API key — skipping think()", self.department, self.role)
            return ""

        system = self.system_prompt
        if context:
            ctx_str = json.dumps(context, ensure_ascii=False, indent=2, default=str)
            system += f"\n\n<context>\n{ctx_str}\n</context>"

        if _sdk_client:
            return self._think_sdk(system, prompt, max_tokens)
        return self._think_cli(system, prompt)

    def _think_sdk(self, system: str, prompt: str, max_tokens: int) -> str:
        """Через Anthropic SDK (работает на Lovable, Railway, VPS с API ключом)."""
        try:
            client: Any = _sdk_client
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text if resp.content else ""
        except Exception as e:
            logger.error("[%s/%s] SDK error: %s", self.department, self.role, e)
            return ""

    def _think_cli(self, system: str, prompt: str) -> str:
        """Через claude CLI (работает локально без API ключа)."""
        full_input = f"{system}\n\n---\n\n{prompt}"
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", full_input, "--model", MODEL],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd="/tmp",  # избегаем CLAUDE.md из проекта
                env={**os.environ},
            )
            output = result.stdout.strip()
            if result.returncode != 0 and not output:
                logger.error("[%s/%s] CLI error: %s", self.department, self.role, result.stderr[:200])
            return output
        except subprocess.TimeoutExpired:
            logger.error("[%s/%s] CLI timeout after %ds", self.department, self.role, TIMEOUT)
            return ""
        except Exception as e:
            logger.error("[%s/%s] CLI error: %s", self.department, self.role, e)
            return ""

    def think_json(self, prompt: str, context: dict | None = None, max_tokens: int = 2048) -> dict[str, Any]:
        """Вызывает Claude и парсит JSON из ответа."""
        full_prompt = prompt + "\n\nОтветь ТОЛЬКО валидным JSON без пояснений."
        raw = self.think(full_prompt, context, max_tokens)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if match:
            raw = match.group(1).strip()
        try:
            result: Any = json.loads(raw)
            if isinstance(result, dict):
                return result
            return {}
        except json.JSONDecodeError:
            obj_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
            if obj_match:
                try:
                    result = json.loads(obj_match.group(1))
                    if isinstance(result, dict):
                        return result
                    return {}
                except Exception:
                    pass
            logger.warning("Could not parse JSON from: %s…", raw[:100])
            return {}
