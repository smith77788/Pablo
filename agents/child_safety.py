"""
Guardian — ИИ-агент защиты детей для Telegram-бота BASIC.FOOD.

Оценивает сообщения на признаки вербовки/груминга/эксплуатации
несовершеннолетних, блокирует их в чатах, которые администрирует бот, и
складывает доказательства в очередь на проверку человеком.

ГРАНИЦЫ (см. tools/child_safety_tools.py):
  - не хранит и не пересылает сам материал (только метаданные);
  - блокировка возможна только там, где бот — админ;
  - внешние отчёты в органы подаёт человек после проверки.
"""
from __future__ import annotations

from agents.base import BaseAgent
from tools.child_safety_tools import (
    scan_text,
    record_evidence,
    enforce_in_managed_chat,
    list_pending_reports,
    build_authority_report,
    HIGH_RISK_THRESHOLD,
)


SYSTEM = """Ты — Guardian, модуль защиты детей внутри Telegram-бота.

ЗАДАЧА:
- Выявлять сообщения с признаками вербовки, груминга или эксплуатации
  несовершеннолетних (сексуальной, торговли людьми, «на органы»).
- Блокировать такой контент в чатах, где бот является администратором.
- Фиксировать доказательства (ТОЛЬКО метаданные и фрагмент текста) в очередь
  на проверку человеком.

ЖЁСТКИЕ ПРАВИЛА:
- НИКОГДА не проси, не сохраняй и не пересылай сам материал (фото/видео).
  Только ссылки, юзернеймы, время и текстовые фрагменты как улики.
- Один сработавший сигнал — это НЕ приговор. Учитывай контекст, возможные
  ложные срабатывания (цитирование новостей, обсуждение защиты детей,
  художественный текст). Сомнительное — помечай на проверку, не наказывай.
- Блокируй (удаляй сообщение + бань) только в управляемых чатах и только при
  высокой уверенности. В остальных случаях — фиксируй доказательство.
- Не предлагай атаки, DoS или массовые жалобы. Единственные законные рычаги —
  модерация в своих чатах и передача доказательств в органы.
- Отвечай кратко и по-русски."""


class ChildSafetyAgent(BaseAgent):
    name = "guardian"
    system_prompt = SYSTEM

    def __init__(self) -> None:
        super().__init__()
        self._register_all_tools()

    def _register_all_tools(self) -> None:
        self.register_tool(
            {
                "name": "scan_text",
                "description": "Проверить текст на сигналы вербовки/груминга/эксплуатации. Возвращает score, категории, уровень риска.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            scan_text,
        )
        self.register_tool(
            {
                "name": "record_evidence",
                "description": "Зафиксировать доказательство в очередь на проверку человеком. ТОЛЬКО метаданные (ссылка, юзернейм, время, фрагмент текста, сигналы) — никогда не медиа.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source_type": {"type": "string", "enum": ["chat_message", "channel", "user_report"]},
                        "chat_id": {"type": ["integer", "string"]},
                        "message_id": {"type": "integer"},
                        "channel_username": {"type": "string"},
                        "channel_title": {"type": "string"},
                        "offender_user_id": {"type": "integer"},
                        "offender_username": {"type": "string"},
                        "text_snippet": {"type": "string"},
                        "signals": {"type": "object"},
                        "reporter_note": {"type": "string"},
                    },
                    "required": ["source_type"],
                },
            },
            record_evidence,
        )
        self.register_tool(
            {
                "name": "enforce_in_managed_chat",
                "description": "Удалить сообщение и забанить пользователя в чате, где бот — админ. Применять только при высокой уверенности.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": ["integer", "string"]},
                        "message_id": {"type": "integer"},
                        "user_id": {"type": "integer"},
                    },
                    "required": ["chat_id", "message_id", "user_id"],
                },
            },
            enforce_in_managed_chat,
        )
        self.register_tool(
            {
                "name": "list_pending_reports",
                "description": "Показать очередь доказательств, ожидающих проверки человеком.",
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 50}},
                },
            },
            list_pending_reports,
        )
        self.register_tool(
            {
                "name": "build_authority_report",
                "description": "Собрать текст отчёта для ручной подачи в органы (NCMEC/IWF/киберполиция и др.) по подтверждённому случаю.",
                "input_schema": {
                    "type": "object",
                    "properties": {"report_id": {"type": "string"}},
                    "required": ["report_id"],
                },
            },
            build_authority_report,
        )

    # ------------------------------------------------------------------
    # Прямой конвейер: быстрая проверка сообщения из чата
    # ------------------------------------------------------------------

    def screen_message(self, ctx: dict) -> dict:
        """
        Быстрая (без LLM) проверка входящего сообщения из управляемого чата.
        При высоком риске — блокирует и фиксирует; при среднем — только фиксирует.
        ctx ожидает: chat_id, message_id, user_id, username, text.
        """
        text = ctx.get("text", "") or ""
        scan = scan_text(text)

        outcome: dict = {"scan": scan, "action": "none"}

        if scan["risk"] == "high" and scan["score"] >= HIGH_RISK_THRESHOLD:
            if ctx.get("chat_id") and ctx.get("message_id") and ctx.get("user_id"):
                outcome["enforcement"] = enforce_in_managed_chat(
                    ctx["chat_id"], ctx["message_id"], ctx["user_id"]
                )
                outcome["action"] = "blocked"
            record_evidence(
                source_type="chat_message",
                chat_id=ctx.get("chat_id"),
                message_id=ctx.get("message_id"),
                offender_user_id=ctx.get("user_id"),
                offender_username=ctx.get("username", ""),
                text_snippet=text,
                signals=scan,
                reporter_note="Автоблокировка Guardian: высокий риск.",
            )
            if outcome["action"] != "blocked":
                outcome["action"] = "recorded"

        elif scan["risk"] == "review":
            record_evidence(
                source_type="chat_message",
                chat_id=ctx.get("chat_id"),
                message_id=ctx.get("message_id"),
                offender_user_id=ctx.get("user_id"),
                offender_username=ctx.get("username", ""),
                text_snippet=text,
                signals=scan,
                reporter_note="Помечено Guardian на проверку человеком.",
            )
            outcome["action"] = "recorded"

        return outcome

    # ------------------------------------------------------------------
    # LLM-разбор сложных случаев и очереди на проверку
    # ------------------------------------------------------------------

    def review_case(self, description: str) -> str:
        """Дать LLM разобрать неоднозначный случай и предложить решение."""
        return self.run(
            "Разбери случай и реши: блокировать, зафиксировать на проверку или "
            "оставить без действия. Обоснуй кратко.\n\n" + description
        )

    def summarize_queue(self) -> str:
        """Обзор очереди доказательств для человека-проверяющего."""
        return self.run(
            "Покажи очередь ожидающих проверки доказательств (list_pending_reports), "
            "сгруппируй по категориям и уровню риска, отметь, что требует "
            "первоочередной передачи в органы."
        )
