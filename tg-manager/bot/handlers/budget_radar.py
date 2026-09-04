"""Экран «Экономика флота»: где прямо сейчас утекает бюджет на прокси/аккаунты.

Конкуренты показывают здоровье и статус — но не деньги. Этот экран переводит
состояние флота в рубли: одна большая цифра «сколько утекает в месяц» + топ
конкретных утечек с готовым действием. Данные — из services/budget_radar
(user_proxies + tg_accounts + operation_audit), ставки — из platform_settings
или дефолтов, так что цифра есть сразу.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, ProxyCb
from bot.utils.op_helpers import safe_answer
from services import budget_radar

log = logging.getLogger(__name__)
router = Router()

_MAX_LISTED = 8  # не заваливаем экран — только самые дорогие утечки


def _render(rep: dict) -> str:
    cur = rep.get("currency", "₽")
    fm = lambda x: budget_radar.fmt_money(x, cur)  # noqa: E731

    total = int(rep.get("proxies_total") or 0)
    if not total and not (rep.get("burned") or {}).get("count"):
        return ("💸 <b>Экономика флота</b>\n\nПрокси в пуле нет. Добавьте прокси и "
                "аккаунты — и радар покажет, где утекает бюджет.")

    monthly = rep.get("monthly_waste") or 0
    annual = rep.get("annual_waste") or 0
    leaks = rep.get("leaks") or []
    burned = rep.get("burned") or {}

    head = ["💸 <b>Экономика флота</b>", ""]
    if monthly > 0:
        head.append(f"🩸 Утекает в месяц: <b>{fm(monthly)}</b>  (в год ≈ {fm(annual)})")
    else:
        head.append("✅ Явных утечек по прокси не нашли — бюджет расходуется в дело.")
    head.append(
        f"<i>прокси в пуле: {total} · утечек: {len(leaks)}</i>")

    if burned.get("count"):
        head.append(
            f"🔥 Сгоревшие регистрации за {burned.get('window', 30)} дн: "
            f"<b>{burned['count']}</b> → {fm(burned.get('cost') or 0)} "
            f"<i>(умерли в первые {burned.get('days', 3)} дн)</i>")

    lines = list(head)
    if leaks:
        lines += ["", "<b>Где горит</b>"]
        for lk in leaks[:_MAX_LISTED]:
            proxy = html.escape(str(lk.get("proxy") or "—"))
            tot = int(lk.get("assigned_total") or 0)
            dead = int(lk.get("assigned_dead") or 0)
            meta = f"аккаунтов {tot}"
            if dead:
                meta += f" (мёртвых {dead})"
            lines.append(
                f"{lk['label']} · <b>{fm(lk.get('monthly_waste') or 0)}/мес</b>\n"
                f"   <code>{proxy}</code> · {meta}\n"
                f"   → <i>{html.escape(lk['fix'])}</i>")
        if len(leaks) > _MAX_LISTED:
            lines.append(f"\n… и ещё {len(leaks) - _MAX_LISTED} — сначала показаны самые дорогие")

    cost = rep.get("cost") or {}
    lines += [
        "",
        (f"<i>Считаем по ставкам: прокси {fm(cost.get('proxy_monthly') or 0)}/мес, "
         f"аккаунт {fm(cost.get('account_unit') or 0)}. Подгоняются в настройках "
         f"(budget_proxy_monthly_cost / budget_account_cost / budget_currency).</i>"),
    ]
    return "\n".join(lines)


def _kb(rep: dict):
    kb = InlineKeyboardBuilder()
    # Если есть подтверждённо-мёртвые НЕназначенные прокси — даём быстрый вычист.
    kb.button(text="🧹 Удалить мёртвые прокси", callback_data=ProxyCb(action="cleanup_dead"))
    kb.button(text="🔄 Обновить", callback_data=BotCb(action="budget"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(1, 2)
    return kb.as_markup()


async def _show(target, pool, owner_id: int, *, edit: bool) -> None:
    try:
        rep = await budget_radar.scan(pool, owner_id)
        text = _render(rep)
        markup = _kb(rep)
    except Exception:
        log.exception("budget_radar: сбор отчёта упал owner=%s", owner_id)
        text = "💸 <b>Экономика флота</b>\n\n⚠️ Не удалось собрать отчёт — попробуйте ещё раз."
        markup = _kb({})
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("budget"))
async def cmd_budget(message: Message, pool) -> None:
    await _show(message, pool, message.from_user.id, edit=False)


@router.callback_query(BotCb.filter(F.action == "budget"))
async def cb_budget(callback: CallbackQuery, pool) -> None:
    await safe_answer(callback)
    await _show(callback.message, pool, callback.from_user.id, edit=True)
