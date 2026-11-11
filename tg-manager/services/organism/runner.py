"""Жизненный цикл организма: сердцебиение + проактивные нуджи.

Раньше «мозг» существовал только когда пользователь открывал экран. Здесь у
организма появляется ПУЛЬС: раз в N минут он смотрит на мир каждого владельца и,
если видит срочное (хранилище отвалилось, флот под давлением, операция упала),
сам пишет в ЛС — с кулдауном, чтобы не спамить. Так система «живёт» и «понимает»
между сессиями, а не ждёт, пока её откроют.

Кулдаун и антиспам держим в общем состоянии (organism_state.last_nudge).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

INTERVAL = 15 * 60          # сердцебиение — раз в 15 мин
_SAME_COOLDOWN = 6 * 3600   # та же подсказка не чаще раза в 6ч
_ANY_GAP = 2 * 3600         # любой нудж не чаще раза в 2ч
_NUDGE_SEV = ("urgent", "warn")
_last_prune = 0.0          # троттл ретеншена журнала (не чаще раза в 6ч)
_SEEN_KEY = "nudge_subjects"   # {id подсказки: [про кого уже сказали]}
_SEEN_CAP = 300


def already_told(suggestion: dict, seen: dict) -> bool:
    """Подсказка про тех же людей, о которых уже было напоминание.

    Сравниваем множества: если все нынешние «субъекты» уже были в прошлом
    нудже этой подсказки — нового ничего нет (кто-то мог и отпасть: ответили,
    купил). Новый человек или новое сообщение того же клиента — повод сказать.
    Подсказки без субъектов (состояние системы) живут по старому кулдауну.
    """
    subj = suggestion.get("subjects")
    if not subj:
        return False
    prev = set((seen or {}).get(suggestion.get("id")) or ())
    return bool(prev) and set(subj) <= prev


async def run(pool, bot) -> None:
    log.info("organism.runner: starting (heartbeat %ds)", INTERVAL)
    while True:
        try:
            await _tick(pool, bot)
        except Exception:
            log.exception("organism.runner: tick failed")
        await asyncio.sleep(INTERVAL)


_OWNERS_SQL = (
    "SELECT owner_id FROM tg_accounts WHERE is_active AND owner_id IS NOT NULL "
    "UNION SELECT added_by AS owner_id FROM managed_bots "
    "WHERE is_active AND added_by IS NOT NULL "
    "UNION SELECT owner_id FROM virtual_states WHERE owner_id IS NOT NULL"
)
_OWNERS_SQL_FALLBACK = (
    "SELECT DISTINCT owner_id FROM tg_accounts "
    "WHERE is_active AND owner_id IS NOT NULL"
)


async def _active_owners(pool) -> list[int]:
    """Кого обслуживает сердцебиение.

    Список брался ровно из `tg_accounts`, а такт делает три разные работы:
    нуджи мозга, каскады виртуального слоя и дозор роста. Владелец, который
    работает только ботами и контактами (TG-аккаунтов не импортировал), в
    список не попадал ВООБЩЕ: его воронка не остывала каскадом, «ходят по
    кругу» не находилось, аномалии роста не замечались, и нудж не приходил
    никогда — организм для него просто не жил. Берём объединение тех
    источников, которые такт и обслуживает.
    """
    try:
        rows = await pool.fetch(_OWNERS_SQL)
    except Exception:
        # Старая схема без одной из таблиц не должна останавливать пульс
        # целиком: падаем до прежнего списка, а не до пустого.
        log.debug("organism.runner: объединение владельцев не удалось",
                  exc_info=True)
        rows = await pool.fetch(_OWNERS_SQL_FALLBACK)
    return [int(r["owner_id"]) for r in rows if r["owner_id"] is not None]


async def _tick(pool, bot) -> int:
    """Один проход по всем владельцам. Возвращает число отправленных нуджей."""
    global _last_prune
    now = time.time()
    if now - _last_prune > 6 * 3600:      # ретеншен журнала — не чаще раза в 6ч
        _last_prune = now
        try:
            from services.organism import spine
            await spine.prune_events(pool, days=30)
        except Exception:
            pass
    # Распад виртуальных состояний: интерес без подтверждения остывает сам.
    # На КАЖДОМ сердцебиении, а не на шестичасовом проходе: «готов купить»
    # живёт сутки, и шести часов задержки хватало, чтобы экран и каскад
    # считали по состояниям, которые уже неверны. Выборка идёт по частичному
    # индексу (только просроченные) и в спокойном состоянии не находит ничего.
    cooled_owners: set[int] = set()
    try:
        from services import virtual_layer
        cooled = await virtual_layer.run_decay(pool, owners_out=cooled_owners)
        if cooled:
            log.info("organism.runner: остыло состояний %d у %d владельцев",
                     cooled, len(cooled_owners))
    except Exception:
        log.debug("organism.runner: virtual_layer decay failed", exc_info=True)

    sent = 0
    _do_cascade = now - _last_prune < 1      # тот же редкий проход, что прунинг
    for oid in await _active_owners(pool):
        try:
            if await _tick_owner(pool, bot, oid):
                sent += 1
        except Exception:
            log.debug("organism.runner: owner %s failed", oid, exc_info=True)
        # Еженедельный отчёт: сам решает, пора ли (окно понедельника + отметка
        # недели), поэтому вызывать его на каждом такте дёшево и безопасно.
        try:
            from services import weekly_report
            await weekly_report.maybe_send(pool, bot, oid)
            await weekly_report.maybe_send_daily(pool, bot, oid)
        except Exception:
            log.debug("organism.runner: weekly report failed owner=%s", oid, exc_info=True)
        # Каскад аудитории: свернуть состояния всех контактов владельца в единую
        # «температуру» (много «горячих» → аудитория горячая). Чистого маппинга
        # контакт→бот нет, поэтому катим на уровень аудитории — это и есть
        # пример «147 горячих → кампания горячая». Тихо: пишет только на смену
        # вердикта.
        #
        # Считаем редким проходом — это не горячий путь — НО обязательно ещё и
        # тогда, когда у этого владельца только что остыли состояния: распад
        # изменил факты, на которых стоит записанный вердикт. Раньше условие
        # было только по редкому проходу, и «аудитория горячая» жила в базе до
        # шести часов после того, как греть стало некого, — а читают этот
        # вердикт экран сводки, подсказки мозга и выбор «самого горячего».
        if _do_cascade or oid in cooled_owners:
            try:
                from services import virtual_layer as _vl
                # Пороги «горячей аудитории» — из правил владельца (экран
                # «Правила модели»); без правил — прежние 20 человек и 15%.
                _rules = await _vl.get_rules(pool, oid)
                await _vl.recompute_cascade(
                    pool, oid, _vl.NETWORK, "audience", _vl.USER,
                    **_vl.cascade_kwargs(_rules, "audience"))
            except Exception:
                log.debug("organism.runner: cascade failed owner=%s", oid, exc_info=True)
            # Каскад «бот ← люди». Раньше его было не из чего построить: связи
            # контакт→бот не существовало. Теперь податель сигнала пишет её в
            # source состояния ("bot_<id>"), и температура считается по каждому
            # боту отдельно: владелец видит, ГДЕ именно греется аудитория, а не
            # только что она греется вообще.
            try:
                from services import virtual_layer as _vl2
                bot_temp = await _vl2.recompute_bot_cascade(
                    pool, oid, **_vl2.cascade_kwargs(
                        await _vl2.get_rules(pool, oid), "bot"))
                if bot_temp:
                    log.info("organism.runner: температура ботов owner=%s: %s",
                             oid, bot_temp)
            except Exception:
                log.debug("organism.runner: bot cascade failed owner=%s", oid,
                          exc_info=True)
        if _do_cascade:
            # Невидимый триггер «заходил и уходил». Telegram такого события не
            # шлёт и прислать не может: он знает отдельные сообщения, а не то,
            # что человек третий раз подходит к покупке и отходит. Считается по
            # истории переходов слоя; отметка живёт отдельным ключом, поэтому
            # событие рождается один раз, а не на каждом проходе.
            try:
                from services import virtual_layer as _vl3
                _bounced = await _vl3.detect_bouncing(pool, oid)
                if _bounced:
                    log.info("organism.runner: ходят по кругу owner=%s: %d",
                             oid, len(_bounced))
            except Exception:
                log.debug("organism.runner: bouncing failed owner=%s", oid,
                          exc_info=True)
            # Дозор роста: запомнить новые всплески/обвалы подписчиков (дедуп внутри).
            try:
                from services import growth_sentry
                found = await growth_sentry.scan_and_remember(pool, oid)
                if found:
                    log.info("organism.runner: аномалий роста owner=%s: %d", oid, len(found))
            except Exception:
                log.debug("organism.runner: growth_sentry failed owner=%s", oid, exc_info=True)
    if sent:
        log.info("organism.runner: нуджей отправлено %d", sent)
    return sent


def _open_button(action: dict | None):
    """web_app-кнопка «Открыть» — прямо в нужный раздел мини-аппа.

    Без неё нудж был тупиком: «ответьте клиенту», но чтобы ПРОЧИТАТЬ, надо вручную
    открыть приложение и искать раздел. Ведём на URL#<kind>; мини-апп разворачивает
    kind тем же runPulseAction, что и карточки Пульса. Возвращает (text, WebAppInfo)
    или None, если URL мини-аппа не настроен.
    """
    kind = (action or {}).get("kind")
    if not kind:
        return None
    try:
        from aiogram.types import WebAppInfo
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL
        url = _valid_mini_app_url(MINI_APP_URL)
        if not url:
            return None
        # Формат #kind[:param] — мини-апп ведёт в раздел и (для operation) сразу к
        # нужной операции. op_id прежде отбрасывался → «Открыть» вело в приложение,
        # а не к операции.
        _param = (action or {}).get("op_id")
        frag = f"{kind}:{_param}" if _param not in (None, "") else kind
        return ("👉 Открыть и ответить" if kind == "vault" else "👉 Открыть",
                WebAppInfo(url=f"{url}#{frag}"))
    except Exception:
        return None


def _snooze_kb(suggestion_id: str, action: dict | None = None):
    """Кнопка «Открыть» (в нужный раздел) + кнопки «заглушить» под нуджем.

    Без snooze уведомление нечем выключить: оно повторяется каждые 6 часов, и
    единственной альтернативой было отключить бота целиком. Периоды — из
    brain.SNOOZE_PRESETS (один источник правды с обработчиком колбэка).
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import SnoozeCb
    from services.organism import brain

    kb = InlineKeyboardBuilder()
    ob = _open_button(action)
    if ob:
        kb.button(text=ob[0], web_app=ob[1])
    # Ждущих ответа можно разобрать и не открывая приложение: список диалогов
    # с карточками и «Ответить» живёт прямо в боте.
    in_bot = (action or {}).get("kind") == "vault"
    if in_bot:
        from bot.callbacks import VaultCb
        kb.button(text="💬 Ждущие ответа — в боте",
                  callback_data=VaultCb(a="list", f="waiting"))
    for code, label, _secs in brain.SNOOZE_PRESETS:
        kb.button(text=f"🔕 {label}",
                  callback_data=SnoozeCb(action="mute", sid=suggestion_id, code=code))
    kb.button(text="🚫 Больше не напоминать",
              callback_data=SnoozeCb(action="off", sid=suggestion_id, code="never"))
    # Открыть (если есть) — отдельной строкой сверху, затем snooze-периоды 2×2.
    head = ([1] if ob else []) + ([1] if in_bot else [])
    kb.adjust(*head, 2, 2, 1)
    return kb.as_markup()


async def _tick_owner(pool, bot, owner_id: int, *, notifier=None, now: float | None = None) -> bool:
    """Проверить одного владельца и при необходимости толкнуть. notifier/now —
    seam для тестов. Возвращает True, если отправили нудж."""
    from services.organism import brain, spine
    now = time.time() if now is None else now
    pulse = await brain.pulse(pool, owner_id)
    # Про тех же самых людей второй раз не напоминаем. Раньше «1 клиентов
    # ждут ответа» приходило утром и снова вечером про одного и того же
    # человека, а «8 ходят по кругу» — каждые 6 часов про одну и ту же
    # восьмёрку: кулдаун знал только id подсказки, а не про кого она.
    try:
        seen = await spine.state_get(pool, owner_id, _SEEN_KEY, {}) or {}
    except Exception:
        seen = {}
    top = next((s for s in pulse.get("suggestions", [])
                if s["severity"] in _NUDGE_SEV and not already_told(s, seen)), None)
    if not top:
        return False
    last = await spine.state_get(pool, owner_id, "last_nudge", {}) or {}
    last_at = float(last.get("at") or 0)
    # антиспам — только если нудж уже был: та же подсказка не чаще 6ч; любая — 2ч.
    if last_at > 0:
        if last.get("id") == top["id"] and now - last_at < _SAME_COOLDOWN:
            return False
        if now - last_at < _ANY_GAP:
            return False
    msg = f"🫀 <b>{top['title']}</b>\n{top['why']}"
    try:
        if notifier:
            await notifier(owner_id, msg)
        else:
            # Общий слой: тумблер «Советы», уровень «инфо» (тихие часы и
            # «только сводкой» уважаются).
            from database import db as _db
            if not await _db.notify_if_enabled(
                    pool, bot, owner_id, "advice", msg,
                    _snooze_kb(top["id"], top.get("action")), dedup_key=top["id"]):
                return False
    except Exception:
        log.debug("organism.runner: notify failed owner=%s", owner_id)
        return False
    await spine.state_set(pool, owner_id, "last_nudge", {"id": top["id"], "at": now})
    if top.get("subjects"):
        try:
            seen = dict(seen)
            seen[top["id"]] = list(top["subjects"])[:_SEEN_CAP]
            await spine.state_set(pool, owner_id, _SEEN_KEY, seen)
        except Exception:
            log.debug("organism.runner: seen-subjects not saved owner=%s", owner_id)
    # Куда ведёт «Открыть» — дублируем в состояние. Кнопка кладёт раздел в
    # #<kind> web_app-ссылки, но ЧАСТЬ клиентов Telegram стирает наш хэш и
    # заменяет его своими tgWebApp-параметрами: раздел теряется, и «Открыть»
    # молча приводит владельца на главную (ровно эта жалоба). Мини-апп на
    # старте, не найдя раздела в хэше, забирает цель отсюда — расход разовый и
    # в узком окне (чтобы обычное открытие приложения спустя час не прыгало в
    # старый нудж). Формат совпадает с _open_button: kind + необязательный param.
    _act = top.get("action") or {}
    _kind = _act.get("kind")
    if _kind:
        _open = {"kind": str(_kind), "at": now}
        _param = _act.get("op_id")
        if _param not in (None, ""):
            _open["param"] = str(_param)
        await spine.state_set(pool, owner_id, "nudge_open", _open)
    return True
