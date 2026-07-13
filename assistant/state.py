"""Persistent state for the assistant bot: admins, model, chat histories.

Everything is stored as plain JSON under data/assistant/ so the bot survives
restarts without external dependencies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from assistant.ai_chat import default_model

DEFAULT_ADMIN_ID = 391641532  # владелец
DEFAULT_MODEL = default_model()  # OpenRouter model id (env OPENROUTER_MODEL or Claude)

STATE_DIR = Path(os.environ.get("ASSISTANT_STATE_DIR", "data/assistant"))
STATE_FILE = STATE_DIR / "state.json"

# Keep histories bounded so requests stay fast and cheap.
MAX_HISTORY_MESSAGES = 60


def _env_admin_ids() -> set[int]:
    raw = os.environ.get("ASSISTANT_ADMIN_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def trim_history(history: list[dict], limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Drop oldest messages past the limit without breaking tool_use pairing.

    A history must start with a plain user message — never with a tool_result
    turn — so we only cut at indices where that holds.
    """
    if len(history) <= limit:
        return history
    start = len(history) - limit
    while start < len(history):
        msg = history[start]
        content = msg.get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if msg.get("role") == "user" and not is_tool_result:
            return history[start:]
        start += 1
    return []


class BotState:
    """Admins, active model, and per-chat Claude histories."""

    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.admins: set[int] = {DEFAULT_ADMIN_ID} | _env_admin_ids()
        self.model: str = DEFAULT_MODEL
        self.histories: dict[int, list[dict]] = {}
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.admins |= {int(a) for a in data.get("admins", [])}
                self.model = data.get("model") or self.model
            except (json.JSONDecodeError, ValueError, OSError):
                pass
        for path in STATE_DIR.glob("history_*.json"):
            try:
                chat_id = int(path.stem.removeprefix("history_"))
                self.histories[chat_id] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError, OSError):
                continue

    def save(self) -> None:
        STATE_FILE.write_text(
            json.dumps(
                {"admins": sorted(self.admins), "model": self.model},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _save_history(self, chat_id: int) -> None:
        path = STATE_DIR / f"history_{chat_id}.json"
        try:
            path.write_text(
                json.dumps(self.histories.get(chat_id, []), ensure_ascii=False),
                encoding="utf-8",
            )
        except (TypeError, OSError):
            # Non-serializable block sneaked in — history stays in-memory only.
            pass

    # -- admins ---------------------------------------------------------

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def grant(self, user_id: int) -> None:
        self.admins.add(user_id)
        self.save()

    def revoke(self, user_id: int) -> bool:
        if user_id == DEFAULT_ADMIN_ID:
            return False
        self.admins.discard(user_id)
        self.save()
        return True

    # -- model ------------------------------------------------------------

    def set_model(self, model: str) -> None:
        self.model = model
        self.save()

    # -- history ---------------------------------------------------------

    def history(self, chat_id: int) -> list[dict]:
        return self.histories.setdefault(chat_id, [])

    def update_history(self, chat_id: int, history: list[dict]) -> None:
        self.histories[chat_id] = trim_history(history)
        self._save_history(chat_id)

    def reset_history(self, chat_id: int) -> None:
        self.histories[chat_id] = []
        self._save_history(chat_id)
