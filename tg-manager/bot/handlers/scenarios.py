"""/scenarios — сценарии автоматизации прямо в чате с ботом.

Раньше о сценариях можно было узнать только в мини-аппе: упал вебхук, бот
отключился — и владелец, получивший оповещение о сбое в чат, шёл искать
сценарий по экранам. Здесь — сводка за сутки по каждому сценарию (запущено,
завершено, упало, цели) и действия, которые нужны сразу после оповещения:
повторить упавшие и поставить сценарий на паузу или включить.

Все действия — через доменные функции движка (`services/workflow_engine`),
с проверкой владельца по `owner_id`: чужой сценарий по номеру из кнопки не
поставить на паузу и не повторить.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ScenCb
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_LIMIT = 15


async def summary(pool, owner_id: int) -> list[dict]:
    """Сценарии владельца со счётчиками за сутки. Зеркала старых воронок
    не показываются: ими управляют в разделе воронок."""
    rows = await pool.fetch(
        """SELECT d.id, d.name, COALESCE(d.is_active, FALSE) AS is_active,
                  d.trigger_type,
                  count(r.id) FILTER (WHERE r.started_at > now() - interval '1 day')
                      AS started,
                  count(r.id) FILTER (WHERE r.status = 'completed'
                                        AND r.finished_at > now() - interval '1 day')
                      AS completed,
                  count(r.id) FILTER (WHERE r.status = 'failed'
                                        AND r.finished_at > now() - interval '1 day')
                      AS failed,
                  count(r.id) FILTER (WHERE r.goal_reached_at > now() - interval '1 day')
                      AS goals,
                  count(r.id) FILTER (WHERE r.status IN ('running', 'waiting'))
                      AS live,
                  count(r.id) FILTER (WHERE r.status = 'failed'
                                        AND r.finished_at > now() - interval '7 days')
                      AS failed_week
             FROM workflow_definitions d
             LEFT JOIN workflow_runs r ON r.workflow_id = d.id
            WHERE d.owner_id = $1 AND d.legacy_kind IS NULL
            GROUP BY d.id
            ORDER BY COALESCE(d.is_active, FALSE) DESC, d.id DESC
            LIMIT $2""", owner_id, _LIMIT)
    return [dict(r) for r in rows]


def render(items: list[dict]) -> tuple[str, object]:
    """Текст и клавиатура сводки. ЧИСТАЯ (кроме сборки клавиатуры)."""
    kb = InlineKeyboardBuilder()
    if not items:
        text = ("🤖 <b>Сценарии</b>\n\nСценариев пока нет. Соберите первый в "
                "мини-аппе: Сценарии → «Создать» или шаблон «Приветствие».")
        kb.button(text="🔄 Обновить", callback_data=ScenCb(action="list"))
        return text, kb.as_markup()
    lines = ["🤖 <b>Сценарии</b> · за сутки\n"]
    for it in items:
        state = "▶️" if it["is_active"] else "⏸"
        parts = [f"запущено {it['started']}", f"завершено {it['completed']}"]
        if it["goals"]:
            parts.append(f"🎯 {it['goals']}")
        if it["failed"]:
            parts.append(f"❌ упало {it['failed']}")
        if it["live"]:
            parts.append(f"идёт {it['live']}")
        lines.append(f"{state} <b>#{it['id']} {html.escape(it['name'] or '')}</b>\n"
                     f"    {' · '.join(parts)}")
        if it["failed_week"]:
            kb.button(text=f"🔁 #{it['id']}: повторить упавшие ({it['failed_week']})",
                      callback_data=ScenCb(action="retry", wf_id=it["id"]))
        kb.button(text=(f"⏸ #{it['id']}: пауза" if it["is_active"]
                        else f"▶️ #{it['id']}: включить"),
                  callback_data=ScenCb(action="pause" if it["is_active"] else "resume",
                                       wf_id=it["id"]))
    kb.button(text="🔄 Обновить", callback_data=ScenCb(action="list"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _show(target: Message, pool, owner_id: int, *, edit: bool) -> None:
    try:
        text, markup = render(await summary(pool, owner_id))
    except Exception:
        log.exception("scenarios: сводка owner=%s", owner_id)
        text, markup = "🤖 <b>Сценарии</b>\n\n⚠️ Не удалось собрать сводку — попробуйте ещё раз.", None
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("scenarios"))
async def cmd_scenarios(message: Message, pool) -> None:
    await _show(message, pool, message.from_user.id, edit=False)


@router.callback_query(ScenCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, pool) -> None:
    await safe_answer(callback)
    await _show(callback.message, pool, callback.from_user.id, edit=True)


@router.callback_query(ScenCb.filter(F.action == "retry"))
async def cb_retry(callback: CallbackQuery, callback_data: ScenCb, pool) -> None:
    from services import workflow_engine as wf
    res = await wf.retry_runs(pool, callback.from_user.id,
                              workflow_id=int(callback_data.wf_id or 0))
    note = (f"🔁 Повторено: {res['retried']}" if res["retried"]
            else "Упавших прогонов за неделю нет")
    if res.get("skipped"):
        note += f" · пропущено: {res['skipped']} (у человека уже идёт этот сценарий)"
    await safe_answer(callback, note, show_alert=True)
    await _show(callback.message, pool, callback.from_user.id, edit=True)


@router.callback_query(ScenCb.filter(F.action.in_({"pause", "resume"})))
async def cb_toggle(callback: CallbackQuery, callback_data: ScenCb, pool) -> None:
    from services import workflow_engine as wf
    fn = wf.pause_workflow if callback_data.action == "pause" else wf.resume_workflow
    try:
        await fn(pool, callback.from_user.id, int(callback_data.wf_id or 0))
        ok = True
    except LookupError:
        ok = False
    await safe_answer(callback, ("⏸ На паузе" if callback_data.action == "pause"
                                 else "▶️ Включён") if ok else "Сценарий не найден")
    await _show(callback.message, pool, callback.from_user.id, edit=True)
