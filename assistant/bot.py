"""Telegram polling loop and command handling for the assistant bot.

Run with:  python main.py assistant   (or  python -m assistant)
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone

import anthropic
import httpx

from assistant import telegram_api as tg
from assistant.claude_chat import ClaudeChat
from assistant.state import BotState, DEFAULT_MODEL

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".sql", ".sh", ".toml", ".ini",
}
IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

HELP_TEXT = """\
🤖 Pablo — твой личный ассистент.

Просто пиши мне — я отвечаю как Claude и могу управлять BASIC.FOOD.
Понимаю фото и документы (PDF, текстовые файлы).

Команды:
/new — начать новый диалог (очистить контекст)
/status — состояние бота
/model — показать/сменить модель Claude
/briefing — утренний брифинг
/orders — обработать новые заказы
/stock — остатки на складе
/weekly — недельный отчёт
/admins — список админов
/grant <id> — выдать доступ
/revoke <id> — забрать доступ
/id — показать твой Telegram ID
/help — эта справка
"""


class AssistantBot:
    def __init__(self) -> None:
        self.state = BotState()
        self.chat = ClaudeChat()
        self.started_at = datetime.now(timezone.utc)
        self.turns_handled = 0

    # -- lifecycle -------------------------------------------------------

    def _handshake(self) -> None:
        """Reset old connections and announce presence.

        Retries forever on transient network/API errors so a hiccup at boot
        never kills the process — the bot simply keeps trying until Telegram
        answers.
        """
        attempt = 0
        while True:
            try:
                tg.reset_old_connections()
                me = tg.get_me()
                logger.info(
                    "Assistant bot online: @%s (id=%s)",
                    me.get("username"),
                    me.get("id"),
                )
                self._notify_admins(
                    f"🟢 Pablo на связи (@{me.get('username')}).\n"
                    "Старые подключения к боту отключены. Напиши /help."
                )
                return
            except Exception as e:
                attempt += 1
                delay = min(60, 2 ** min(attempt, 6))
                logger.warning(
                    "Startup handshake failed (attempt %s): %s — retry in %ss",
                    attempt, e, delay,
                )
                time.sleep(delay)

    def serve(self) -> None:
        """One lifecycle: handshake, then poll forever.

        The poll loop swallows every per-iteration error so it can never exit
        on its own; the outer supervisor in run() is a second safety net that
        restarts serve() if this ever returns or raises anyway.
        """
        self._handshake()

        offset = 0
        while True:
            try:
                updates = tg.get_updates(offset=offset)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    logger.warning(
                        "getUpdates conflict (409): токен всё ещё использует другой "
                        "сервис. Отзови токен через @BotFather (/revoke) и обнови "
                        "ASSISTANT_BOT_TOKEN."
                    )
                    time.sleep(10)
                else:
                    logger.error("Telegram poll error: %s", e)
                    time.sleep(5)
                continue
            except Exception as e:
                logger.error("Telegram poll error: %s", e)
                time.sleep(5)
                continue

            if not isinstance(updates, list):
                logger.error("Unexpected getUpdates payload: %r", updates)
                time.sleep(5)
                continue

            for update in updates:
                try:
                    offset = update["update_id"] + 1
                    self._handle_update(update)
                except Exception:
                    logger.exception("Failed to handle update %s", update.get("update_id"))
                    chat_id = (update.get("message") or {}).get("chat", {}).get("id")
                    if chat_id:
                        try:
                            tg.send_message(chat_id, "⚠️ Что-то пошло не так. Попробуй ещё раз.")
                        except Exception:
                            pass

    def _notify_admins(self, text: str) -> None:
        for admin_id in self.state.admins:
            try:
                tg.send_message(admin_id, text)
            except Exception as e:
                # 403 = admin hasn't pressed Start yet — expected, not fatal
                logger.warning("Cannot notify admin %s: %s", admin_id, e)

    # -- update routing ----------------------------------------------------

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        user_id = user.get("id")

        if not self.state.is_admin(user_id):
            logger.info("Rejected non-admin user %s (@%s)", user_id, user.get("username"))
            tg.send_message(
                chat_id,
                f"⛔ Доступ ограничен.\nТвой Telegram ID: {user_id}\n"
                "Попроси владельца выдать доступ командой /grant.",
            )
            return

        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            self._handle_command(chat_id, user_id, text)
            return

        content = self._build_user_content(msg)
        if content is None:
            tg.send_message(
                chat_id,
                "Этот тип сообщения я пока не понимаю. "
                "Пришли текст, фото или документ (PDF/текст).",
            )
            return

        self._claude_turn(chat_id, content)

    def _claude_turn(self, chat_id: int, content: str | list[dict]) -> None:
        tg.send_chat_action(chat_id, "typing")
        try:
            reply, history = self.chat.run_turn(
                model=self.state.model,
                history=self.state.history(chat_id),
                user_content=content,
                on_progress=lambda: tg.send_chat_action(chat_id, "typing"),
            )
        except anthropic.AuthenticationError:
            logger.error("Claude auth failed — check ANTHROPIC_API_KEY")
            tg.send_message(
                chat_id,
                "⚠️ Claude API отклонил ключ. Проверь переменную ANTHROPIC_API_KEY "
                "в Railway (действующий ключ Claude). Команды вроде /status работают "
                "и без него.",
            )
            return
        except anthropic.APIError as e:
            logger.exception("Claude API error")
            tg.send_message(chat_id, f"⚠️ Claude API временно недоступен ({e}). Попробуй ещё раз.")
            return
        self.state.update_history(chat_id, history)
        self.turns_handled += 1
        tg.send_message(chat_id, reply)

    # -- attachments -----------------------------------------------------

    def _build_user_content(self, msg: dict) -> str | list[dict] | None:
        text = msg.get("text") or ""
        caption = msg.get("caption") or ""

        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
            data, _ = tg.download_file(file_id)
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(data).decode(),
                    },
                },
                {"type": "text", "text": caption or "Что на этом фото?"},
            ]

        if msg.get("document"):
            return self._document_content(msg["document"], caption)

        if any(msg.get(k) for k in ("voice", "audio", "video", "video_note", "sticker")):
            return None

        if text:
            return text
        return None

    def _document_content(self, doc: dict, caption: str) -> list[dict] | None:
        name = (doc.get("file_name") or "file").lower()
        mime = doc.get("mime_type") or ""
        data, _ = tg.download_file(doc["file_id"])
        prompt = caption or f"Посмотри файл {doc.get('file_name')} и опиши, что в нём."

        if mime == "application/pdf" or name.endswith(".pdf"):
            return [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(data).decode(),
                    },
                },
                {"type": "text", "text": prompt},
            ]

        if mime in IMAGE_MIME:
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.standard_b64encode(data).decode(),
                    },
                },
                {"type": "text", "text": prompt},
            ]

        if any(name.endswith(ext) for ext in TEXT_EXTENSIONS) or mime.startswith("text/"):
            try:
                body = data.decode("utf-8")
            except UnicodeDecodeError:
                body = data.decode("utf-8", errors="replace")
            return [
                {
                    "type": "text",
                    "text": f"Файл {doc.get('file_name')}:\n\n{body[:150000]}\n\n{prompt}",
                }
            ]

        return None

    # -- commands ----------------------------------------------------------

    def _handle_command(self, chat_id: int, user_id: int, text: str) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        if cmd == "/start":
            tg.send_message(chat_id, "Привет! Я Pablo — твой ассистент. 🤝\n\n" + HELP_TEXT)
        elif cmd == "/help":
            tg.send_message(chat_id, HELP_TEXT)
        elif cmd == "/id":
            tg.send_message(chat_id, f"Твой Telegram ID: {user_id}")
        elif cmd == "/new":
            self.state.reset_history(chat_id)
            tg.send_message(chat_id, "🧹 Контекст очищен. Начинаем с чистого листа.")
        elif cmd == "/status":
            uptime = datetime.now(timezone.utc) - self.started_at
            history_len = len(self.state.history(chat_id))
            tg.send_message(
                chat_id,
                "📊 Статус\n"
                f"- Модель: {self.state.model}\n"
                f"- Аптайм: {str(uptime).split('.')[0]}\n"
                f"- Обработано сообщений: {self.turns_handled}\n"
                f"- Сообщений в контексте этого чата: {history_len}\n"
                f"- Админов: {len(self.state.admins)}",
            )
        elif cmd == "/model":
            self._cmd_model(chat_id, args)
        elif cmd == "/admins":
            ids = "\n".join(f"- {a}" for a in sorted(self.state.admins))
            tg.send_message(chat_id, f"👥 Админы:\n{ids}")
        elif cmd == "/grant":
            self._cmd_grant(chat_id, args)
        elif cmd == "/revoke":
            self._cmd_revoke(chat_id, args)
        elif cmd in ("/briefing", "/orders", "/stock", "/weekly"):
            self._cmd_business(chat_id, cmd)
        else:
            tg.send_message(chat_id, "Не знаю такую команду. /help — список команд.")

    def _cmd_model(self, chat_id: int, args: list[str]) -> None:
        if not args:
            tg.send_message(
                chat_id,
                f"Текущая модель: {self.state.model}\n\n"
                "Сменить: /model <id>, например:\n"
                f"- {DEFAULT_MODEL} (по умолчанию)\n"
                "- claude-sonnet-5 (быстрее и дешевле)\n"
                "- claude-haiku-4-5 (самая быстрая)",
            )
            return
        model = args[0]
        if not model.startswith("claude-"):
            tg.send_message(chat_id, "ID модели должен начинаться с claude-")
            return
        self.state.set_model(model)
        tg.send_message(chat_id, f"✅ Модель переключена: {model}")

    def _cmd_grant(self, chat_id: int, args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            tg.send_message(chat_id, "Использование: /grant <telegram_id>")
            return
        new_id = int(args[0])
        self.state.grant(new_id)
        tg.send_message(chat_id, f"✅ Доступ выдан: {new_id}")

    def _cmd_revoke(self, chat_id: int, args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            tg.send_message(chat_id, "Использование: /revoke <telegram_id>")
            return
        target = int(args[0])
        if self.state.revoke(target):
            tg.send_message(chat_id, f"✅ Доступ отозван: {target}")
        else:
            tg.send_message(chat_id, "Нельзя отозвать доступ у владельца.")

    def _cmd_business(self, chat_id: int, cmd: str) -> None:
        tg.send_chat_action(chat_id, "typing")
        prompts = {
            "/briefing": "Сделай утренний брифинг.",
            "/orders": "Обработай новые заказы.",
            "/stock": "Проверь остатки на складе.",
            "/weekly": "Сделай недельный отчёт.",
        }
        self._claude_turn(chat_id, prompts[cmd])


def run() -> None:
    """Supervisor: keep the bot alive no matter what.

    Anything that escapes serve() — a crash while building the bot (missing
    ANTHROPIC_API_KEY, no network at boot), an unexpected exception in the
    poll loop — is caught here and the whole thing is restarted with
    exponential backoff. The process only exits on Ctrl-C / SIGTERM.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("==== Pablo assistant bot: supervisor starting ====")

    backoff = 1
    while True:
        started = time.monotonic()
        try:
            AssistantBot().serve()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Assistant bot stopped by signal.")
            return
        except Exception:
            logger.exception("Assistant bot crashed — restarting")

        # A long run means the failure was transient; reset the backoff so a
        # bot that stayed up for hours restarts instantly, while a boot loop
        # (missing key/network) backs off up to a minute between attempts.
        if time.monotonic() - started > 60:
            backoff = 1
        logger.warning("Restarting assistant bot in %ss…", backoff)
        time.sleep(backoff)
        backoff = min(60, backoff * 2)
