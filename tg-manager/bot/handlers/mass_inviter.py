"""Инвайтер — массовое добавление пользователей в группу.

Источники:
  1. Из парсера аудитории (база parsed_audiences)
  2. Вручную @username / ID (через запятую)
  3. По номерам телефонов
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import InviterCb, BmCb
from bot.keyboards import subscription_locked_markup
from bot.utils.subscription import locked_text
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


class InviterFSM(StatesGroup):
    group = State()        # куда добавляем
    source = State()       # откуда берём (ждём callback выбора)
    users_manual = State() # @username/ID вручную
    phones = State()       # телефоны
    acc_count = State()    # кол-во аккаунтов
    preflight_group = State()  # проверка готовности флота: ждём группу


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    return kb.as_markup()


async def _acc_count(pool: asyncpg.Pool, owner_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
        owner_id,
    )
    return int(row["cnt"]) if row else 0


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "menu"))
async def cb_inviter_menu(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await state.clear()
    total_accs = await _acc_count(pool, callback.from_user.id)
    total_parsed = await pool.fetchval(
        "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1",
        callback.from_user.id,
    ) or 0
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить пользователей", callback_data=InviterCb(action="start"))
    kb.button(text="🔍 Проверить готовность флота", callback_data=InviterCb(action="preflight"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)
    await _edit(
        callback,
        "👥 <b>Инвайтер</b>\n\n"
        "Массовое добавление пользователей в группу через ваши аккаунты.\n\n"
        f"🔑 Аккаунтов: <b>{total_accs}</b>\n"
        f"🗃 В базе парсера: <b>{total_parsed}</b> пользователей\n\n"
        "Поддерживает:\n"
        "• @username / user_id — вручную\n"
        "• Из базы парсера аудитории\n"
        "• По номерам телефонов\n\n"
        "🔍 <i>Перед запуском проверьте готовность флота к нужной группе — "
        "кто подключится, кто в группе, есть ли админ.</i>",
        kb.as_markup(),
    )


@router.callback_query(InviterCb.filter(F.action == "joinall"))
async def cb_inviter_joinall(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    group = data.get("pf_group", "")
    if not group:
        await callback.answer("Группа не задана — запустите проверку заново.", show_alert=True)
        return
    await callback.answer("⏳ Вступаю всеми аккаунтами…")
    try:
        await callback.message.edit_text(
            f"⏳ Вступаю аккаунтами в <code>{html.escape(group)}</code>… "
            "(может занять время)", parse_mode="HTML")
    except Exception:
        log_exc_swallow(log, "joinall: edit wait")
    from services.invite_preflight import join_all
    try:
        r = await join_all(pool, callback.from_user.id, group)
    except Exception:
        log_exc_swallow(log, "joinall run failed")
        await callback.message.answer("⚠️ Не удалось выполнить вступление. Попробуйте позже.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Проверить готовность", callback_data=InviterCb(action="preflight"))
    kb.button(text="➕ Запустить инвайт", callback_data=InviterCb(action="start"))
    kb.button(text="◀️ В меню", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            f"🚪 <b>Вступление в</b> <code>{html.escape(group)}</code>\n\n"
            f"✅ Вступили: <b>{r['joined']}</b>\n"
            f"➖ Уже были в группе: <b>{r['already']}</b>\n"
            f"❌ Не удалось: <b>{r['failed']}</b>\n"
            f"Всего аккаунтов: <b>{r['total']}</b>\n\n"
            "<i>Теперь можно запускать инвайт (прямой метод требует членства).</i>",
            parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        log_exc_swallow(log, "joinall: edit result")


@router.callback_query(InviterCb.filter(F.action == "preflight"))
async def cb_inviter_preflight(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InviterFSM.preflight_group)
    await _edit(
        callback,
        "🔍 <b>Проверка готовности флота</b>\n\n"
        "Введите @username или ссылку группы — проверю ваши аккаунты: кто "
        "подключится, кто уже в группе, кто админ (сможет раздать права).\n\n"
        "<i>Ничего не меняю, только читаю. Может занять до минуты на большой флот.</i>",
        _cancel_kb(),
    )


@router.message(InviterFSM.preflight_group)
async def msg_inviter_preflight(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.mass_inviter_engine import parse_group_ref
    group = parse_group_ref(message.text or "")
    if not group:
        await message.answer("⚠️ Не удалось распознать группу. Введите @username или t.me/...")
        return
    # Не чистим data — сохраняем группу для возможного «Вступить всеми».
    await state.set_state(None)
    await state.update_data(pf_group=group)
    wait = await message.answer("⏳ Проверяю готовность флота… (подключаю аккаунты)")
    from services.invite_preflight import run_preflight
    try:
        rep = await run_preflight(pool, message.from_user.id, group)
    except Exception:
        log_exc_swallow(log, "preflight run failed")
        await wait.edit_text("⚠️ Не удалось выполнить проверку. Попробуйте позже.")
        return
    c = rep["counts"]
    lines = [
        f"🔍 <b>Готовность флота к</b> <code>{html.escape(group)}</code>\n",
        f"Проверено аккаунтов: <b>{rep['total']}</b>\n",
        f"👑 Админы (могут раздать права): <b>{c['admin']}</b>",
        f"✅ В группе (участники): <b>{c['member']}</b>",
        f"🚪 Не в группе: <b>{c['not_member']}</b>",
        f"❌ Не подключились (сессия/сеть): <b>{c['no_connect']}</b>",
    ]
    if c["group_bad"]:
        lines.append(f"🚫 Группа недоступна: <b>{c['group_bad']}</b>")
    if c["error"]:
        lines.append(f"⚠️ Прочие ошибки: <b>{c['error']}</b>")
    lines.append(f"\n🎯 Итого готовы инвайтить: <b>{rep['ready']}</b>")
    lines.append(f"\n💡 {rep['verdict']}")
    kb = InlineKeyboardBuilder()
    # Если есть не-участники — предложим вступить всеми (для прямого инвайта нужно
    # членство). Группа уже сохранена в FSM (pf_group).
    if c["not_member"]:
        kb.button(text=f"🚪 Вступить всеми в группу ({c['not_member']} не в группе)",
                  callback_data=InviterCb(action="joinall"))
    kb.button(text="➕ Запустить инвайт", callback_data=InviterCb(action="start"))
    kb.button(text="🔁 Проверить снова", callback_data=InviterCb(action="preflight"))
    kb.button(text="◀️ В меню", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    try:
        await wait.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


# ── Шаг 1: Выбор группы ──────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "start"))
async def cb_inviter_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InviterFSM.group)
    await _edit(
        callback,
        "👥 <b>Инвайтер — шаг 1/3</b>\n\n"
        "Введите @username или ссылку группы, <b>куда</b> добавляем пользователей:\n\n"
        "<i>Аккаунты должны уже быть участниками этой группы.</i>",
        _cancel_kb(),
    )


@router.message(InviterFSM.group)
async def msg_inviter_group(message: Message, state: FSMContext) -> None:
    from services.mass_inviter_engine import parse_group_ref
    group = parse_group_ref(message.text or "")
    if not group:
        await message.answer("⚠️ Не удалось распознать группу. Введите @username или t.me/...")
        return
    await state.update_data(group=group)
    await state.set_state(InviterFSM.source)
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Из базы парсера", callback_data=InviterCb(action="src_parser"))
    kb.button(text="📇 Из хранилища контактов (CRM)", callback_data=InviterCb(action="src_crm"))
    kb.button(text="✏️ Вручную (@username/ID)", callback_data=InviterCb(action="src_manual"))
    kb.button(text="📱 По номерам телефонов", callback_data=InviterCb(action="src_phones"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ Группа: <code>{html.escape(group)}</code>\n\n"
        "<b>Откуда брать пользователей?</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Шаг 2a: Из парсера аудитории ─────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_parser"))
async def cb_src_parser(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    owner_id = callback.from_user.id
    # Показываем последние 5 парсингов
    runs = await pool.fetch(
        "SELECT parse_run_id, source_title, COUNT(*) AS cnt "
        "FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id IS NOT NULL "
        "GROUP BY parse_run_id, source_title ORDER BY parse_run_id DESC LIMIT 8",
        owner_id,
    )
    if not runs:
        await callback.answer("⚠️ База парсера пуста. Сначала запустите парсер.", show_alert=True)
        return
    await state.update_data(source_type="parser")
    kb = InlineKeyboardBuilder()
    for r in runs:
        label = f"{r['source_title'] or 'без названия'} ({r['cnt']} чел.)"
        kb.button(text=label[:50], callback_data=InviterCb(action="pick_run", item=str(r["parse_run_id"])))
    kb.button(text="🌐 Вся база", callback_data=InviterCb(action="pick_run", item="all"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "📋 Выберите источник из парсера:", kb.as_markup())


@router.callback_query(InviterCb.filter(F.action == "pick_run"))
async def cb_pick_run(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    run_id = callback_data.item
    owner_id = callback.from_user.id
    if run_id == "all":
        count = await pool.fetchval(
            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1", owner_id
        )
        await state.update_data(parse_run_id=None, total_users=count)
    else:
        count = await pool.fetchval(
            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
            owner_id, int(run_id),
        )
        await state.update_data(parse_run_id=int(run_id), total_users=count)

    # Смычка с модулем «Определение пола»: предложить фильтр/разметку перед
    # выбором аккаунтов.
    await _inv_offer_gender(callback, state, pool, count)


async def _inv_offer_gender(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, count: int
) -> None:
    """Шаг пола для выбранной parsed-базы инвайта: фильтр / определить / без фильтра."""
    owner_id = callback.from_user.id
    data = await state.get_data()
    _pr = data.get("parse_run_id")
    _where = "owner_id=$1" + (" AND parse_run_id=$2" if _pr else "")
    _args = [owner_id] + ([_pr] if _pr else [])
    try:
        m_cnt = await pool.fetchval(
            f"SELECT COUNT(*) FROM parsed_audiences WHERE {_where} AND gender='m'", *_args) or 0
        f_cnt = await pool.fetchval(
            f"SELECT COUNT(*) FROM parsed_audiences WHERE {_where} AND gender='f'", *_args) or 0
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM parsed_audiences WHERE {_where}", *_args) or 0
    except Exception:
        m_cnt = f_cnt = total = 0

    if m_cnt or f_cnt:
        kb = InlineKeyboardBuilder()
        kb.button(text="👥 Все", callback_data=InviterCb(action="gender", item="all"))
        if m_cnt:
            kb.button(text=f"👨 Только муж. ({m_cnt:,})", callback_data=InviterCb(action="gender", item="m"))
        if f_cnt:
            kb.button(text=f"👩 Только жен. ({f_cnt:,})", callback_data=InviterCb(action="gender", item="f"))
        kb.adjust(1)
        await _edit(
            callback,
            "🚻 <b>Фильтр по полу</b>\n\nВ базе есть разметка пола. Кого приглашать?\n"
            "<i>(разметка приблизительная — по имени)</i>",
            kb.as_markup())
        return

    if total > 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="🚻 Определить пол", callback_data=InviterCb(action="classify"))
        kb.button(text="➡️ Без фильтра", callback_data=InviterCb(action="gender", item="all"))
        kb.adjust(1)
        await _edit(
            callback,
            "🚻 <b>Пол в этой базе не размечен</b>\n\nМожно определить пол по именам "
            "и приглашать только нужный пол — или продолжить без фильтра.",
            kb.as_markup())
        return

    await state.update_data(inv_gender=None)
    await state.set_state(InviterFSM.acc_count)
    await _ask_acc_count(callback, data, count, pool)


@router.callback_query(InviterCb.filter(F.action == "classify"))
async def cb_inviter_classify(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Определить пол в выбранной базе на месте, затем вернуть к шагу фильтра."""
    await callback.answer("⏳ Определяю пол...")
    from services import gender_classifier as gc
    data = await state.get_data()
    _pr = data.get("parse_run_id") or None
    try:
        await gc.classify_audience(pool, callback.from_user.id, _pr)
    except Exception:
        log_exc_swallow(log, "invite classify failed")
        await callback.answer("Ошибка разметки пола", show_alert=True)
        return
    await _inv_offer_gender(callback, state, pool, data.get("total_users", 0))


@router.callback_query(InviterCb.filter(F.action == "gender"))
async def cb_inviter_gender(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    g = callback_data.item if callback_data.item in ("m", "f") else None
    await state.update_data(inv_gender=g)
    data = await state.get_data()
    # Пересчитать под фильтр — честный total для выбора числа аккаунтов.
    _pr = data.get("parse_run_id")
    _where = "owner_id=$1" + (" AND parse_run_id=$2" if _pr else "")
    _args = [callback.from_user.id] + ([_pr] if _pr else [])
    if g:
        _where += f" AND gender=${len(_args) + 1}"
        _args.append(g)
    try:
        count = await pool.fetchval(
            f"SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE {_where}", *_args) or 0
    except Exception:
        count = data.get("total_users", 0)
    await state.update_data(total_users=count)
    await state.set_state(InviterFSM.acc_count)
    await _ask_acc_count(callback, data, count, pool)


# ── Шаг 2в: Из хранилища контактов (CRM) ─────────────────────────────────────
#
# Хранилище контактов (unified_contacts) — это отдельная база CRM, которая до сих
# пор НЕ была источником инвайта: инвайтер умел парсер, ручной ввод и телефоны, а
# накопленные контакты — нет. Здесь мы даём выбрать сегмент CRM (вся база /
# избранные / тег) и отдаём его в тот же конвейер инвайта, что и остальные
# источники — со всей его флуд-защитой (карантин, готовность аккаунта, дневные
# лимиты, паузы). Никакого нового пути к аккаунтам не создаём: цели — из CRM,
# исполнение — через существующую операцию mass_invite.


def _crm_invitable_where(owner_id: int, crm_filter: dict | None) -> tuple[str, list]:
    """WHERE для контактов, ПРИГОДНЫХ к инвайту, + аргументы.

    ОДИН источник условия для подсчёта и для материализации целей: если считать
    одним запросом, а выбирать другим — оператор увидит одно число, а пригласит
    другое. Пригоден = есть хоть один идентификатор: @username, telegram_user_id
    или непустой телефон.
    """
    where = ("owner_id=$1 AND (telegram_user_id IS NOT NULL "
             "OR (username IS NOT NULL AND username <> '') "
             "OR jsonb_array_length(COALESCE(phones, '[]'::jsonb)) > 0)")
    args: list = [owner_id]
    kind = (crm_filter or {}).get("kind")
    if kind == "fav":
        where += " AND is_favorite = TRUE"
    elif kind == "tag":
        args.append((crm_filter or {}).get("tag") or "")
        where += f" AND ${len(args)} = ANY(tags)"
    return where, args


def _crm_split_targets(rows) -> tuple[list, list]:
    """Разложить контакты на user_refs и phones, предпочитая ДЕШЁВЫЙ идентификатор.

    Порядок предпочтения на контакт: @username → числовой id → телефон. Телефон
    — только когда ни username, ни id нет: импорт контакта дороже и рискованнее
    (лишний ImportContacts на аккаунт), поэтому не тащим номер там, где хватает
    username. Дедуп сквозной, чтобы один и тот же человек не попал дважды.
    """
    import json as _json
    user_refs: list = []
    phones: list = []
    seen: set = set()
    for r in rows:
        uname = (r["username"] or "").strip().lstrip("@")
        uid = r["telegram_user_id"]
        ref = f"@{uname}" if uname else (str(uid) if uid else None)
        if ref:
            key = ref.lower()
            if key not in seen:
                seen.add(key)
                user_refs.append(ref)
            continue
        raw = r["phones"]
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                raw = []
        for p in (raw or []):
            p = str(p).strip() if p else ""
            if p and p not in seen:
                seen.add(p)
                phones.append(p)
                break
    return user_refs, phones


@router.callback_query(InviterCb.filter(F.action == "src_crm"))
async def cb_src_crm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    owner_id = callback.from_user.id
    where, args = _crm_invitable_where(owner_id, None)
    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM unified_contacts WHERE {where}", *args) or 0
    if total == 0:
        await callback.answer(
            "⚠️ В хранилище контактов нет никого, кого можно пригласить "
            "(нужен @username, ID или телефон). Сначала соберите контакты.",
            show_alert=True)
        return
    fav_where, fav_args = _crm_invitable_where(owner_id, {"kind": "fav"})
    fav = await pool.fetchval(
        f"SELECT COUNT(*) FROM unified_contacts WHERE {fav_where}", *fav_args) or 0
    # Топ тегов среди ПРИГОДНЫХ контактов — тот же предикат, чтобы счётчик тега
    # совпадал с тем, что реально пригласится.
    tag_rows = await pool.fetch(
        f"SELECT t AS tag, COUNT(*) AS cnt FROM unified_contacts, unnest(tags) AS t "
        f"WHERE {where} GROUP BY t ORDER BY cnt DESC, t LIMIT 6", *args)
    tags = [r["tag"] for r in tag_rows]
    await state.update_data(source_type="crm", crm_tags=tags)

    kb = InlineKeyboardBuilder()
    kb.button(text=f"🌐 Вся база ({total} чел.)",
              callback_data=InviterCb(action="pick_crm", item="all"))
    if fav:
        kb.button(text=f"⭐ Избранные ({fav})",
                  callback_data=InviterCb(action="pick_crm", item="fav"))
    for i, r in enumerate(tag_rows):
        kb.button(text=f"🏷 {r['tag']} ({r['cnt']})"[:60],
                  callback_data=InviterCb(action="pick_crm", item=f"t{i}"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        "📇 <b>Из хранилища контактов (CRM)</b>\n\n"
        "Кого приглашаем? Выберите сегмент.\n"
        "<i>Приглашаем по @username или ID, где они есть; телефон — только если "
        "другого идентификатора нет.</i>",
        kb.as_markup())


@router.callback_query(InviterCb.filter(F.action == "pick_crm"))
async def cb_pick_crm(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext,
    pool: asyncpg.Pool
) -> None:
    owner_id = callback.from_user.id
    data = await state.get_data()
    item = callback_data.item
    if item == "fav":
        crm_filter = {"kind": "fav"}
    elif item.startswith("t") and item[1:].isdigit():
        tags = data.get("crm_tags") or []
        idx = int(item[1:])
        if idx < 0 or idx >= len(tags):
            await callback.answer("⚠️ Сегмент не найден — начните заново", show_alert=True)
            return
        crm_filter = {"kind": "tag", "tag": tags[idx]}
    else:
        crm_filter = {}          # "all"

    where, args = _crm_invitable_where(owner_id, crm_filter)
    count = await pool.fetchval(
        f"SELECT COUNT(*) FROM unified_contacts WHERE {where}", *args) or 0
    if count == 0:
        await callback.answer("⚠️ В этом сегменте некого приглашать", show_alert=True)
        return
    await state.update_data(crm_filter=crm_filter, total_users=count)
    await state.set_state(InviterFSM.acc_count)
    await _ask_acc_count(callback, data, count, pool)


# ── Шаг 2b: Вручную ──────────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_manual"))
async def cb_src_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(source_type="manual")
    await state.set_state(InviterFSM.users_manual)
    await _edit(
        callback,
        "✏️ Введите @username или user_id через запятую или с новой строки:\n\n"
        "<code>@user1, @user2\n123456789\n@user3</code>\n\n"
        "📎 Или пришлите <b>файлом</b>: .txt, .csv, .xlsx или база .db (SQLite). "
        "Ограничение на объём снято — приму хоть десятки тысяч.",
        _cancel_kb(),
    )


@router.message(InviterFSM.users_manual, F.text)
async def msg_inviter_manual(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.mass_inviter_engine import parse_user_refs
    refs = parse_user_refs(message.text or "")
    if not refs:
        await message.answer("⚠️ Не удалось распознать пользователей. Введите @username "
                             "или числовые ID — либо пришлите файл .txt/.csv/.xlsx/.db.")
        return
    await _accept_refs(message, state, pool, refs, phones=False)


@router.message(InviterFSM.users_manual, F.document)
async def msg_inviter_manual_file(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    refs = await _refs_from_document(message, mode="refs")
    if refs is None:
        return
    if not refs:
        await message.answer("⚠️ В файле не нашлось ни одного @username / ID.")
        return
    await _accept_refs(message, state, pool, refs, phones=False)


# ── Шаг 2c: По телефонам ─────────────────────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "src_phones"))
async def cb_src_phones(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(source_type="phones")
    await state.set_state(InviterFSM.phones)
    await _edit(
        callback,
        "📱 Введите номера телефонов через запятую или с новой строки:\n\n"
        "<code>+79991234567\n+7 999 123 45 67\n89991234567</code>\n\n"
        "📎 Или пришлите <b>файлом</b>: .txt, .csv, .xlsx или база .db (SQLite). "
        "Ограничение на объём снято.",
        _cancel_kb(),
    )


@router.message(InviterFSM.phones, F.text)
async def msg_inviter_phones(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.mass_inviter_engine import parse_phones
    phones = parse_phones(message.text or "")
    if not phones:
        await message.answer("⚠️ Не удалось распознать номера. Формат +79991234567 — "
                             "либо пришлите файл .txt/.csv/.xlsx/.db.",
                             reply_markup=_cancel_kb())
        return
    await _accept_refs(message, state, pool, phones, phones=True)


@router.message(InviterFSM.phones, F.document)
async def msg_inviter_phones_file(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    phones = await _refs_from_document(message, mode="phones")
    if phones is None:
        return
    if not phones:
        await message.answer("⚠️ В файле не нашлось ни одного номера телефона.")
        return
    await _accept_refs(message, state, pool, phones, phones=True)


# ── Общие помощники приёма списка (текст/файл) ───────────────────────────────

async def _count_already_invited(pool: asyncpg.Pool, owner_id: int, group_key: str,
                                 items: list) -> int:
    """Сколько из списка УЖЕ приглашались в эту группу (по invite_target_log).

    Fail-open: любая ошибка/огромный список → 0 (превью необязательно, инвайт
    всё равно дедупит на исполнении)."""
    if not group_key or not items or len(items) > 50_000:
        return 0
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM invite_target_log "
            "WHERE owner_id=$1 AND group_key=$2 AND target = ANY($3::text[])",
            owner_id, group_key, [str(x) for x in items])
        return int(n or 0)
    except Exception:
        return 0


async def _accept_refs(message: Message, state: FSMContext, pool: asyncpg.Pool,
                       items: list, phones: bool) -> None:
    """Сохранить распознанный список и перейти к выбору числа аккаунтов."""
    if phones:
        await state.update_data(phones=items, total_users=len(items))
    else:
        await state.update_data(user_refs=items, total_users=len(items))
    await state.set_state(InviterFSM.acc_count)
    data = await state.get_data()
    word = "номеров" if phones else "пользователей"
    text = f"✅ Принято: <b>{len(items)}</b> {word}."
    # Превью дедупа: сколько уже приглашались в эту группу (их пропустим).
    already = await _count_already_invited(pool, message.from_user.id,
                                           data.get("group", ""), items)
    if already:
        text += (f"\n♻️ Уже приглашались в эту группу: <b>{already}</b> — пропущу.\n"
                 f"🆕 Новых к приглашению: <b>{len(items) - already}</b>.")
    await message.answer(text, parse_mode="HTML")
    await _ask_acc_count_msg(message, data, len(items), pool)


async def _refs_from_document(message: Message, mode: str):
    """Скачать документ и разобрать в список. None при ошибке (уже ответили)."""
    from services import invite_list_parser as ilp
    doc = message.document
    if not doc:
        await message.answer("⚠️ Пришлите файл документом.")
        return None
    if doc.file_size and doc.file_size > ilp.MAX_FILE_BYTES:
        await message.answer(f"⚠️ Файл слишком большой "
                             f"(лимит {ilp.MAX_FILE_BYTES // (1024 * 1024)} МБ).",
                             reply_markup=_cancel_kb())
        return None
    try:
        f = await message.bot.get_file(doc.file_id)
        buf = await message.bot.download_file(f.file_path)
        data = buf.read() if hasattr(buf, "read") else bytes(buf)
    except Exception as e:
        log.warning("inviter: download file failed: %s", e)
        await message.answer("⚠️ Не удалось скачать файл. Попробуйте ещё раз.")
        return None
    try:
        return ilp.parse_invite_file(doc.file_name or "list.txt", data, mode=mode)
    except ValueError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}")
        return None


# ── Шаг 3: Кол-во аккаунтов → подтверждение ─────────────────────────────────

async def _ask_acc_count(cb: CallbackQuery, data: dict, user_count: int, pool: asyncpg.Pool):
    total = await _acc_count(pool, cb.from_user.id)
    await _edit(
        cb,
        f"✅ Пользователей: <b>{user_count}</b>\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов задействовать? (0 = все):",
        _cancel_kb(),
    )


async def _ask_acc_count_msg(msg: Message, data: dict, user_count: int, pool: asyncpg.Pool):
    total = await _acc_count(pool, msg.from_user.id)
    await msg.answer(
        f"✅ Пользователей: <b>{user_count}</b>\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов задействовать? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(InviterFSM.acc_count)
async def msg_inviter_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    owner_id = message.from_user.id
    total = await _acc_count(pool, owner_id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    data = await state.get_data()
    group = data.get("group", "")
    source_type = data.get("source_type", "manual")
    total_users = data.get("total_users", 0)

    source_label = {
        "parser": f"База парсера ({total_users} чел.)",
        "crm": f"Хранилище контактов ({total_users} чел.)",
        "manual": f"Вручную ({total_users} чел.)",
        "phones": f"По телефонам ({total_users} чел.)",
    }.get(source_type, str(source_type))

    # Распределяем пользователей по аккаунтам
    per_acc = max(1, (total_users + use - 1) // use)

    kb = InlineKeyboardBuilder()
    # Способ инвайта. «Обычный» — прямое добавление (нужны права add_users).
    # «Через админку» — трюк: цель делается админом (это добавляет её в чат),
    # затем права тут же снимаются, и пользователь остаётся участником. Обходит
    # приватность «кто может добавлять» и работает от любого админа с правом
    # «Назначать администраторов». Именно так добавляют «упрямых» пользователей.
    kb.button(text="🛡 Безопасный (умный темп по чату)",
              callback_data=InviterCb(action="method", item="safe"))
    kb.button(text="➕ Обычный инвайт", callback_data=InviterCb(action="method", item="direct"))
    kb.button(text="👑 Через админку (обход приватности)",
              callback_data=InviterCb(action="method", item="admin"))
    kb.button(text="🔗 Рассылка ссылки в ЛС",
              callback_data=InviterCb(action="method", item="link"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "👥 <b>Инвайтер — способ добавления</b>\n\n"
        f"🎯 Группа: <code>{html.escape(group)}</code>\n"
        f"📋 Источник: {html.escape(source_label)}\n"
        f"🔑 Аккаунтов: <b>{use}</b>\n"
        f"📊 ~{per_acc} пользователей на аккаунт\n\n"
        "• <b>Обычный</b> — прямое добавление (аккаунту нужны права приглашать).\n"
        "• <b>Через админку</b> — пользователь делается админом (попадает в чат) "
        "и тут же лишается прав → остаётся участником. Обходит приватность.\n"
        "• <b>Ссылка в ЛС</b> — рассылаем каждому ссылку-приглашение, человек "
        "вступает сам. Полностью обходит приватность, безопаснее всего, но "
        "вступление не гарантировано.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _inv_offer_pace(message: Message, data: dict) -> None:
    """Экран выбора темпа (после выбора способа инвайта)."""
    group = data.get("group", "")
    use = data.get("acc_count", 1)
    total_users = data.get("total_users", 0)
    method = data.get("inv_method", "direct")
    per_acc = max(1, (total_users + use - 1) // use)
    _m_ru = {"admin": "👑 через админку", "link": "🔗 ссылка в ЛС"}.get(method, "➕ обычный")

    kb = InlineKeyboardBuilder()
    # Темп = пауза между батчами. Инвайт — самая баноопасная операция, поэтому
    # выбор скорости обязателен (медленный безопаснее для аккаунтов).
    kb.button(text="🐢 Медленно (безопасно)", callback_data=InviterCb(action="setpace", item="slow"))
    kb.button(text="🚶 Обычно", callback_data=InviterCb(action="setpace", item="normal"))
    kb.button(text="🐇 Быстро (риск)", callback_data=InviterCb(action="setpace", item="fast"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1, 2, 1)
    await message.answer(
        "👥 <b>Инвайтер — выбор темпа</b>\n\n"
        f"🎯 Группа: <code>{html.escape(group)}</code>\n"
        f"⚙️ Способ: <b>{_m_ru}</b>\n"
        f"🔑 Аккаунтов: <b>{use}</b>\n"
        f"📊 ~{per_acc} пользователей на аккаунт\n\n"
        "⚠️ <i>Инвайт — самая баноопасная операция. «Медленно» "
        "снижает риск ограничений аккаунтов.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _inv_offer_volume(message: Message, data: dict) -> None:
    """Экран выбора объёма на аккаунт за прогон (после темпа)."""
    use = data.get("acc_count", 1)
    total_users = data.get("total_users", 0)
    need = max(1, (total_users + use - 1) // use)  # сколько на аккаунт, чтобы закрыть базу
    kb = InlineKeyboardBuilder()
    # «Авто» — консервативный расчёт по истории аккаунта (безопасно, но у свежих
    # аккаунтов даёт мало). Числа — осознанный фиксированный лимит на аккаунт за
    # прогон (режим «один проход»: не клампим предсказанным дневным лимитом, но
    # держим потолок безопасности 50 и живые сигналы флуда). 50 — верхняя граница.
    kb.button(text="🤖 Авто (по истории, безопасно)", callback_data=InviterCb(action="confirm", item="auto"))
    kb.button(text="🎯 Прогрессивно (по возрасту)", callback_data=InviterCb(action="confirm", item="prog"))
    kb.button(text="📈 10 / аккаунт", callback_data=InviterCb(action="confirm", item="10"))
    kb.button(text="📈 25 / аккаунт", callback_data=InviterCb(action="confirm", item="25"))
    kb.button(text="🚀 50 / аккаунт (максимум)", callback_data=InviterCb(action="confirm", item="50"))
    kb.button(text="❌ Отмена", callback_data=InviterCb(action="menu"))
    kb.adjust(1, 1, 2, 1, 1)
    await message.answer(
        "👥 <b>Инвайтер — объём на аккаунт</b>\n\n"
        f"🔑 Аккаунтов: <b>{use}</b> · в базе <b>{total_users}</b>\n"
        f"📊 Чтобы закрыть базу за один проход, нужно ~<b>{need}</b> на аккаунт.\n\n"
        "• <b>Авто</b> — сколько безопасно по истории аккаунта (у свежих — мало, "
        "≈2–15; растёт по мере чистой работы).\n"
        "• <b>Прогрессивно</b> — бот сам ставит лимит по возрасту аккаунта: свежим "
        "мало (5), через 3 дня — 12, через неделю — 25, дальше до 50. Не выбирать "
        "вручную и не жечь молодые.\n"
        "• <b>Число</b> — фиксированный лимит на аккаунт за прогон (режим «один "
        "проход»). Потолок безопасности — 50: выше почти гарантированный бан.\n\n"
        "⚠️ <i>Чем больше за раз, тем выше риск ограничений. Для свежих аккаунтов "
        "лучше начинать с малого и растить.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(InviterCb.filter(F.action == "setpace"))
async def cb_inviter_pace(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext
) -> None:
    pace = callback_data.item if callback_data.item in ("slow", "normal", "fast") else "normal"
    await state.update_data(inv_pace=pace)
    data = await state.get_data()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _inv_offer_volume(callback.message, data)
    await callback.answer()


@router.callback_query(InviterCb.filter(F.action == "method"))
async def cb_inviter_method(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext
) -> None:
    # «Безопасный» — это обычное добавление (direct) ПОВЕРХ governor'а уровня
    # чата (умный темп/паузы/стоп на мёртвом чате). Флаг независим от метода.
    _item = callback_data.item
    safe = _item == "safe"
    method = _item if _item in ("direct", "admin", "link") else "direct"
    await state.update_data(inv_method=method, inv_safe=safe)
    data = await state.get_data()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _inv_offer_pace(callback.message, data)
    await callback.answer()


# ── Подтверждение и постановка в очередь ─────────────────────────────────────

@router.callback_query(InviterCb.filter(F.action == "confirm"))
async def cb_inviter_confirm(
    callback: CallbackQuery, callback_data: InviterCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    # Темп сохранён на прошлом шаге; здесь item — выбор ОБЪЁМА на аккаунт.
    pace = data.get("inv_pace", "normal")
    # Объём на аккаунт за прогон:
    #   "auto" → лимит по истории аккаунта (per_account_limit=0, без «одного прохода»);
    #   "prog" → прогрессивно по возрасту/доверию (volume_mode=progressive);
    #   число  → фиксированный лимит N на аккаунт в режиме «один проход» (не клампим
    #            консервативным дневным прогнозом, но держим потолок 50 + сигналы флуда).
    _vol = callback_data.item if callback_data.item in ("auto", "prog", "10", "25", "50") else "auto"
    _volume_mode = ""
    if _vol == "auto":
        _per_acc_limit, _one_pass = 0, False
    elif _vol == "prog":
        _per_acc_limit, _one_pass, _volume_mode = 0, False, "progressive"
    else:
        _per_acc_limit, _one_pass = int(_vol), True
    acc_count = data.get("acc_count", 1)
    group = data.get("group", "")
    source_type = data.get("source_type", "manual")

    # Загрузить аккаунты
    rows = await pool.fetch(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
        owner_id, acc_count,
    )
    account_ids = [r["id"] for r in rows]
    if not account_ids:
        await callback.answer("⚠️ Нет доступных аккаунтов", show_alert=True)
        return

    # Собрать список пользователей для добавления
    if source_type == "parser":
        run_id = data.get("parse_run_id")
        _g = data.get("inv_gender")
        _gw = ""
        _gargs: list = []
        if _g in ("m", "f"):
            _gargs.append(_g)
        if run_id:
            if _gargs:
                _gw = " AND gender=$3"
            prows = await pool.fetch(
                "SELECT DISTINCT tg_user_id, username FROM parsed_audiences "
                "WHERE owner_id=$1 AND parse_run_id=$2" + _gw + " LIMIT 100000",
                owner_id, run_id, *_gargs,
            )
        else:
            if _gargs:
                _gw = " AND gender=$2"
            prows = await pool.fetch(
                "SELECT DISTINCT tg_user_id, username FROM parsed_audiences "
                "WHERE owner_id=$1" + _gw + " LIMIT 100000",
                owner_id, *_gargs,
            )
        user_refs = [
            f"@{r['username']}" if r["username"] else str(r["tg_user_id"])
            for r in prows
        ]
        phones: list[str] = []
    elif source_type == "crm":
        # Материализуем цели из CRM ТЕМ ЖЕ предикатом, что и считали на прошлом
        # шаге, — иначе оператор видел одно число, а пригласит другое.
        crm_filter = data.get("crm_filter", {})
        where, cargs = _crm_invitable_where(owner_id, crm_filter)
        crows = await pool.fetch(
            f"SELECT telegram_user_id, username, phones FROM unified_contacts "
            f"WHERE {where} LIMIT 100000", *cargs)
        user_refs, phones = _crm_split_targets(crows)
    elif source_type == "phones":
        user_refs = []
        phones = data.get("phones", [])
    else:
        user_refs = data.get("user_refs", [])
        phones = []

    total_users = len(user_refs) + len(phones)
    if total_users == 0:
        await callback.answer("⚠️ Список пользователей пуст", show_alert=True)
        return

    method = data.get("inv_method", "direct")
    if method not in ("direct", "admin", "link"):
        method = "direct"

    params = {
        "group": group,
        "account_ids": account_ids,
        "user_refs": user_refs,
        "phones": phones,
        "batch_size": 5,
        # Темп (пауза между батчами) + объём на аккаунт из выбора оператора.
        # per_account_limit=0 + one_pass=False → авто по истории; N + one_pass=True →
        # фиксированный лимит на аккаунт за прогон (потолок безопасности 50 держится).
        "pace": pace,
        "per_account_limit": _per_acc_limit,
        "one_pass": _one_pass,
        "volume_mode": _volume_mode,
        # Способ добавления: direct (InviteToChannel) или admin (промоут-трюк).
        "invite_method": method,
        # Безопасный режим: governor уровня чата (частота/мин, заморозка приёма,
        # стоп на мёртвом чате и серии выходов/жалоб).
        "safe_mode": bool(data.get("inv_safe")),
    }
    _pace_ru = {"slow": "🐢 медленно", "normal": "🚶 обычно", "fast": "🐇 быстро"}[pace]
    _method_ru = {"admin": "👑 через админку", "link": "🔗 ссылка в ЛС"}.get(method, "➕ обычный")
    if params["safe_mode"]:
        _method_ru = "🛡 безопасный + " + _method_ru
    _vol_ru = {"auto": "🤖 авто (по истории)",
               "prog": "🎯 прогрессивно (по возрасту)"}.get(_vol, f"{_vol}/аккаунт (один проход)")
    label = f"Инвайтер: {group} ← {total_users} пользователей × {len(account_ids)} акк."
    # operation_bus.submit() — не сырой INSERT: централизует то, что раньше здесь
    # молча обходилось для самой баноопасной операции продукта — план-гейт
    # (mass_invite требует "pro"), дедуп повторной постановки и предохранитель
    # Ban Weather (при шторме банов по этому op_type новые прогоны блокируются).
    # docstring services/operation_bus.py прямо запрещает прямой INSERT в новых
    # хендлерах — этот путь был исключением, которое отменяло все три защиты.
    from services import operation_bus
    from services.operation_bus import PlanRequiredError, ImmunityBlockedError

    try:
        op_id = await operation_bus.submit(
            pool, owner_id, "mass_invite", params,
            total_items=total_users, label=label,
        )
    except PlanRequiredError as exc:
        await _edit(
            callback,
            locked_text("Массовый инвайтинг", exc.required_plan),
            subscription_locked_markup(exc.required_plan, back_callback=InviterCb(action="menu")),
        )
        return
    except ImmunityBlockedError as exc:
        await _edit(
            callback,
            f"🌩 <b>Инвайт временно приостановлен</b>\n\n{html.escape(str(exc))}",
            InlineKeyboardBuilder().button(
                text="◀️ В меню", callback_data=InviterCb(action="menu")
            ).as_markup(),
        )
        return
    except Exception as exc:
        log.exception("mass_invite submit owner=%s", owner_id)
        await callback.answer(f"⚠️ Не удалось поставить в очередь: {str(exc)[:150]}", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ В меню", callback_data=InviterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Инвайтер поставлен в очередь</b>\n\n"
        f"🆔 Операция: <b>#{op_id}</b>\n"
        f"🎯 Группа: <code>{html.escape(group)}</code>\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🔑 Аккаунтов: <b>{len(account_ids)}</b>\n"
        f"⚙️ Способ: <b>{_method_ru}</b>\n"
        f"⏱ Темп: <b>{_pace_ru}</b>\n"
        f"📊 Объём: <b>{_vol_ru}</b>",
        kb.as_markup(),
    )
