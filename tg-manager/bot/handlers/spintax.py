"""Spintax-модуль — авто-генерация и раскрытие spintax-шаблонов.

Точки входа:
  /spin                       — открыть модуль (меню)
  /spin <текст сценария>      — сразу сгенерировать варианты из текста
  SpinCb(action="menu")       — кнопка модуля

Как работает:
  1. Пользователь присылает обычный текст сообщения.
  2. Уже подключённые к платформе LLM-провайдеры (OpenRouter/Groq/Gemini/Ollama)
     переписывают его в 5 максимально разных spintax-шаблонов.
  3. Каждый шаблон проверяется движком ``services.spintax_engine``; наружу уходят
     только валидные, вместе со случайным примером раскрытия.
  4. Кнопка «🔁 Ещё генерация» мгновенно перераскрывает уже готовые шаблоны в
     новые случайные варианты — БЕЗ повторного обращения к LLM (быстро и без
     лишних вызовов). Шаблоны хранятся в FSM (Postgres) до новой генерации.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, SpinCb
from bot.states import SpinFlow
from bot.utils.op_helpers import safe_answer

from services import spintax_service
from services.ai_providers import configured_providers
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_SPIN_COUNT = spintax_service.DEFAULT_SPIN_COUNT
_MAX_SCRIPT_LEN = 2000
_DATA_TEMPLATES = "spin_templates"  # ключ в FSM-данных для хранения шаблонов

_INTRO = (
    "🎲 <b>Spintax-модуль</b>\n\n"
    "Пришлите обычный текст сообщения — я соберу из него "
    f"<b>{_SPIN_COUNT}</b> максимально разных spintax-шаблонов "
    "(синонимы в <code>{{вариант1|вариант2}}</code>) и проверю каждый движком.\n\n"
    "Потом кнопкой «🔁 Ещё генерация» можно мгновенно получать новые случайные "
    "варианты из тех же шаблонов — без повторного обращения к AI.\n\n"
    "✍️ Отправьте текст сценария или нажмите «Раскрыть шаблон», "
    "чтобы прогнать уже готовый spintax.\n\n"
    "Отмена — /cancel"
)


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Новый спин из текста", callback_data=SpinCb(action="new"))
    kb.button(text="🔀 Раскрыть шаблон", callback_data=SpinCb(action="expand"))
    kb.button(text="🏠 Меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb


def _result_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё генерация", callback_data=SpinCb(action="reroll"))
    kb.button(text="✍️ Новый текст", callback_data=SpinCb(action="new"))
    kb.button(text="🏠 Меню", callback_data=BmCb(action="main"))
    kb.adjust(1, 2)
    return kb


async def _ai_complete(system: str, user: str) -> str:
    """Единичный запрос к LLM с перебором провайдеров (OpenAI-совместимый API)."""
    providers = configured_providers()
    if not providers:
        raise spintax_service.SpintaxServiceError(
            "AI не настроен: добавьте OPENROUTER_API_KEY, GROQ_API_KEY или GEMINI_API_KEY"
        )
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise spintax_service.SpintaxServiceError("библиотека openai не установлена") from exc

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for provider in providers:
        for model in provider.models:
            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=30.0,
            )
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=2000,
                    temperature=1.0,
                )
                text = response.choices[0].message.content or ""
                if text.strip():
                    return text
            except Exception as exc:  # noqa: BLE001 - failover по провайдерам
                last_error = exc
                log_exc_swallow(log, f"spin: провайдер {provider.name}/{model} не ответил")
                continue
    raise spintax_service.SpintaxServiceError(
        f"ни один AI-провайдер не ответил: {last_error}" if last_error else "AI недоступен"
    )


# ── Точки входа ──────────────────────────────────────────────────────────────


@router.message(Command("spin"))
async def cmd_spin(message: Message, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await _run_spin(message, state, parts[1].strip())
        return
    await state.set_state(SpinFlow.waiting_script)
    await message.answer(_INTRO, parse_mode="HTML", reply_markup=_menu_kb().as_markup())


@router.callback_query(SpinCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SpinFlow.waiting_script)
    await safe_answer(callback)
    await callback.message.answer(
        _INTRO, parse_mode="HTML", reply_markup=_menu_kb().as_markup()
    )


@router.callback_query(SpinCb.filter(F.action == "new"))
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SpinFlow.waiting_script)
    await safe_answer(callback)
    await callback.message.answer(
        "✍️ Пришлите обычный текст сообщения — соберу из него spintax-шаблоны.\n\n"
        "Отмена — /cancel"
    )


@router.callback_query(SpinCb.filter(F.action == "expand"))
async def cb_expand(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SpinFlow.waiting_expand)
    await safe_answer(callback)
    await callback.message.answer(
        "🔀 Пришлите готовый spintax-шаблон вида "
        "<code>{Привет|Здравствуйте} {мир|друг}</code> — "
        "раскрою его в несколько случайных вариантов.\n\n"
        "Отмена — /cancel",
        parse_mode="HTML",
    )


@router.callback_query(SpinCb.filter(F.action == "reroll"))
async def cb_reroll(callback: CallbackQuery, state: FSMContext) -> None:
    """Быстрая повторная генерация: перераскрываем сохранённые шаблоны без LLM."""
    data = await state.get_data()
    templates = data.get(_DATA_TEMPLATES) or []
    if not templates:
        await safe_answer(
            callback, "Нечего перегенерировать — пришлите текст заново.", show_alert=True
        )
        return
    await safe_answer(callback, "🔁 Новые варианты")
    text = _render_result(templates)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_result_kb().as_markup()
        )
    except Exception:
        # edit может упасть, если содержимое не изменилось — отправим новым сообщением
        log_exc_swallow(log, "spin: не удалось отредактировать сообщение при reroll")
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=_result_kb().as_markup()
        )


@router.message(SpinFlow.waiting_script, F.text)
@router.message(SpinFlow.showing, F.text)
async def msg_script(message: Message, state: FSMContext) -> None:
    await _run_spin(message, state, (message.text or "").strip())


@router.message(SpinFlow.waiting_expand, F.text)
async def msg_expand(message: Message, state: FSMContext) -> None:
    template = (message.text or "").strip()
    if not spintax_service.is_valid_template(template):
        await message.answer(
            "❌ Это не похоже на корректный spintax-шаблон. "
            "Проверьте, что все <code>{</code> закрыты <code>}</code>.",
            parse_mode="HTML",
            reply_markup=_menu_kb().as_markup(),
        )
        return
    # Готовый шаблон тоже сохраняем — «🔁 Ещё генерация» будет работать и здесь.
    await state.set_state(SpinFlow.showing)
    await state.update_data(**{_DATA_TEMPLATES: [template]})
    await message.answer(
        _render_result([template]),
        parse_mode="HTML",
        reply_markup=_result_kb().as_markup(),
    )


# ── Основная генерация ───────────────────────────────────────────────────────


async def _run_spin(message: Message, state: FSMContext, script: str) -> None:
    if not script:
        await message.answer("✍️ Пришлите текст сценария.")
        return
    if len(script) > _MAX_SCRIPT_LEN:
        await message.answer(
            f"⚠️ Слишком длинный текст (лимит {_MAX_SCRIPT_LEN} символов). Сократите сценарий."
        )
        return

    wait = await message.answer("🎲 Генерирую spintax-варианты…")
    try:
        templates = await spintax_service.generate_spins(
            script, complete=_ai_complete, count=_SPIN_COUNT
        )
    except spintax_service.SpintaxServiceError as exc:
        await wait.edit_text(f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        log_exc_swallow(log, "spin: непредвиденная ошибка генерации")
        await wait.edit_text("⚠️ Внутренняя ошибка, попробуйте ещё раз.")
        return

    # Сохраняем шаблоны для мгновенной повторной генерации по кнопке.
    await state.set_state(SpinFlow.showing)
    await state.update_data(**{_DATA_TEMPLATES: templates})
    await wait.edit_text(
        _render_result(templates),
        parse_mode="HTML",
        reply_markup=_result_kb().as_markup(),
    )


def _render_result(templates: list[str]) -> str:
    """Собирает сообщение: случайное раскрытие каждого шаблона + сам шаблон."""
    blocks: list[str] = []
    for i, template in enumerate(templates, 1):
        try:
            sample = spintax_service.expand_template(template)
        except Exception:
            sample = ""
        block = f"<b>{i}.</b> {html.escape(sample)}" if sample else f"<b>{i}.</b>"
        block += f"\n<code>{html.escape(template)}</code>"
        warnings = spintax_service.quality_warnings(template)
        if warnings:
            block += "\n⚠️ " + "; ".join(html.escape(w) for w in warnings)
        blocks.append(block)

    header = (
        f"🎲 <b>Готово: {len(templates)} "
        f"{'шаблон' if len(templates) == 1 else 'вариантов'}</b>\n"
        "Сверху — случайный результат, снизу — сам spintax-шаблон "
        "(можно скопировать в софт).\n\n"
    )
    return header + "\n\n".join(blocks)
