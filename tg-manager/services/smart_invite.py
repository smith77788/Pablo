"""Безопасный инвайтинг: governor уровня ЧАТА (не только аккаунта).

Telegram оценивает не только аккаунт-инвайтера, но и сам чат: частоту системных
событий о вступлении в единицу времени, соотношение живой активности к
«холодному» притоку, ранние выходы и жалобы новоприглашённых. При аномалии он
замораживает ПРИЁМ в чат (chat-level FLOOD_WAIT), и следующий инвайт ломается
независимо от того, каким аккаунтом добавлять.

Что делает этот governor (учтены известные на сегодня механики + свои методы):

  1. Частота/мин по ЧАТУ. Скользящее окно 60 с: не больше N системных вступлений
     в минуту в конкретный чат, сколько бы аккаунтов ни работало. Именно всплеск
     событий в минуту вешает триггер.

  2. Заморозка чата. Если чат ответил flood (или пошла серия негатива) — ставим
     paused_until и не трогаем чат до конца паузы (экспонента по повторам).

  3. Живость чата («мёртвый чат»). Пустой чат без общения ловит флуд после
     первых же вступлений. liveness_score (участники/свежесть активности)
     масштабирует лимит: мёртвый чат — темп режется, совсем мёртвый — стоп.

  4. Ранние выходы/жалобы. Копим joined/left/reported; если доля отвалившихся
     превышает порог на достаточной выборке — операция ОСТАНАВЛИВАЕТСЯ (дальше
     лить в чат, который убивает приглашённых, — гарантированный флуд).

  5. Свои методы: «холодный старт» с малого лимита и ПРОГРЕССИВНЫЙ разогрев —
     лимит растёт только с числом ПОДТВЕРЖДЁННЫХ вступлений без негатива;
     джиттер интервалов (без ровной машинной частоты); строгий backoff после
     flood.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InviteGuardConfig:
    base_per_min: int = 2            # холодный старт: очень осторожно
    max_per_min_cap: int = 8         # потолок даже для «разогретого» живого чата
    joins_per_extra_per_min: int = 5 # каждые N подтверждённых вступлений → +1/мин
    abort_ratio: float = 0.35        # (left+reported)/joined — выше → стоп
    abort_min_sample: int = 8        # не судим на малой выборке
    liveness_floor: float = 0.30     # ниже — темп вдвое; ~0 — стоп
    chat_flood_base_pause_sec: int = 1800   # 30 мин, ×2^(flood_hits-1)
    chat_flood_max_pause_sec: int = 6 * 3600
    inter_invite_base_sec: float = 30.0     # джиттер вокруг этого
    inter_invite_jitter: float = 0.5        # ±50%


DEFAULT_CFG = InviteGuardConfig()


@dataclass(frozen=True)
class InviteDecision:
    allowed: bool
    wait_sec: float          # сколько ждать, если сейчас нельзя
    reason: str
    next_delay_sec: float    # рекомендованная пауза ПОСЛЕ успешного инвайта
    abort: bool = False      # не «подождать», а прекратить лить в этот чат


def _effective_max_per_min(st: dict, cfg: InviteGuardConfig) -> float:
    """Прогрессивный лимит/мин: холодный старт + разогрев по подтверждённым
    вступлениям, срезанный живостью чата."""
    joined = int(st.get("joined_total") or 0)
    grown = cfg.base_per_min + joined / max(1, cfg.joins_per_extra_per_min)
    capped = min(float(cfg.max_per_min_cap), grown)
    live = st.get("liveness_score")
    if live is not None:
        if live <= 0.05:
            return 0.0                      # совсем мёртвый чат — не льём
        if live < cfg.liveness_floor:
            capped *= 0.5                   # мёртвый — темп вдвое
    return max(1.0, capped) if capped > 0 else 0.0


def _negative_ratio(st: dict) -> float:
    joined = int(st.get("joined_total") or 0)
    bad = int(st.get("left_total") or 0) + int(st.get("reported_total") or 0)
    if joined <= 0:
        return 0.0
    return bad / joined


def decide(st: dict, now: datetime, cfg: InviteGuardConfig = DEFAULT_CFG) -> InviteDecision:
    """Чистое решение по состоянию чата: можно ли пригласить прямо сейчас."""
    # 1) Заморозка чата (flood/негатив) — ждём до конца паузы.
    paused = st.get("paused_until")
    if paused is not None and paused > now:
        return InviteDecision(False, (paused - now).total_seconds(),
                              st.get("pause_reason") or "чат на паузе", 0.0)

    # 2) Негативная доля — не «подождать», а ПРЕКРАТИТЬ (чат убивает приглашённых).
    joined = int(st.get("joined_total") or 0)
    if joined >= cfg.abort_min_sample and _negative_ratio(st) > cfg.abort_ratio:
        return InviteDecision(
            False, 0.0,
            "слишком много вышедших/пожаловавшихся — чат «мёртвый», лить дальше "
            "нельзя (гарантированный флуд)", 0.0, abort=True)

    # 3) Живость: совсем мёртвый чат — стоп.
    eff_max = _effective_max_per_min(st, cfg)
    if eff_max <= 0.0:
        return InviteDecision(
            False, 0.0,
            "чат без живой активности — «холодный» приток в него мгновенно ловит "
            "флуд; сначала оживите чат", 0.0, abort=True)

    # 4) Частота/мин по чату: скользящее окно 60 с.
    win_start = st.get("window_start")
    win_count = int(st.get("window_count") or 0)
    if win_start is not None and (now - win_start) < timedelta(seconds=60):
        if win_count >= eff_max:
            wait = 60.0 - (now - win_start).total_seconds()
            return InviteDecision(False, max(1.0, wait),
                                  f"лимит частоты по чату ({int(eff_max)}/мин) — ждём окно", 0.0)

    return InviteDecision(True, 0.0, "ok", _next_delay(eff_max, cfg))


def _next_delay(eff_max: float, cfg: InviteGuardConfig) -> float:
    """Пауза после успешного инвайта: держит темп под лимитом + джиттер, чтобы
    события не шли ровной машинной частотой."""
    base = max(cfg.inter_invite_base_sec, 60.0 / max(1.0, eff_max))
    j = cfg.inter_invite_jitter
    return round(base * random.uniform(1.0 - j, 1.0 + j), 1)


# ── async-обёртки над состоянием в БД ────────────────────────────────────────

_COLS = ("window_start, window_count, paused_until, pause_reason, invited_total, "
         "joined_total, left_total, reported_total, flood_hits, liveness_score, "
         "liveness_checked_at")


async def _load(pool, owner_id: int, chat_key: str) -> dict:
    row = await pool.fetchrow(
        f"SELECT {_COLS} FROM chat_invite_state WHERE owner_id=$1 AND chat_key=$2",
        owner_id, chat_key)
    return dict(row) if row else {}


async def _ensure(pool, owner_id: int, chat_key: str) -> None:
    await pool.execute(
        "INSERT INTO chat_invite_state(owner_id, chat_key) VALUES($1,$2) "
        "ON CONFLICT (owner_id, chat_key) DO NOTHING", owner_id, chat_key)


async def can_invite(pool, owner_id: int, chat_key: str,
                     cfg: InviteGuardConfig = DEFAULT_CFG) -> InviteDecision:
    """Решение по чату из БД-состояния. Fail-open на ошибке чтения (не блокируем
    работу из-за сбоя вспомогательного слоя), но пишем в лог."""
    try:
        st = await _load(pool, owner_id, chat_key)
        return decide(st, datetime.now(timezone.utc), cfg)
    except Exception:
        log_exc_swallow(log, f"smart_invite.can_invite chat={chat_key}")
        return InviteDecision(True, 0.0, "governor недоступен — идём осторожно",
                              cfg.inter_invite_base_sec)


async def note_sent(pool, owner_id: int, chat_key: str, n: int = 1) -> None:
    """Отметить отправку n инвайтов: +n в окно частоты и в invited_total.
    Окно сбрасывается, если прошло больше 60 с.

    Параметр n не косметика. Исполнитель вызывал эту функцию в цикле «по разу на
    цель», и на батч из пяти целей уходило до двадцати обращений к БД чисто на
    учёт — последовательно, внутри цикла инвайта, то есть прямо замедляя прогон.
    На аудитории в 2000 целей это тысячи лишних round-trip'ов. Семантика от
    пакетного инкремента не меняется: пять вызовов подряд и один с n=5
    происходят в одном и том же окне частоты.
    """
    if n <= 0:
        return
    try:
        await _ensure(pool, owner_id, chat_key)
        await pool.execute(
            """UPDATE chat_invite_state SET
                 window_start = CASE
                     WHEN window_start IS NULL OR now() - window_start >= interval '60 seconds'
                     THEN now() ELSE window_start END,
                 window_count = CASE
                     WHEN window_start IS NULL OR now() - window_start >= interval '60 seconds'
                     THEN $3 ELSE window_count + $3 END,
                 invited_total = invited_total + $3,
                 updated_at = now()
               WHERE owner_id=$1 AND chat_key=$2""",
            owner_id, chat_key, int(n))
    except Exception:
        log_exc_swallow(log, f"smart_invite.note_sent chat={chat_key}")


async def note_outcome(pool, owner_id: int, chat_key: str, outcome: str,
                       cfg: InviteGuardConfig = DEFAULT_CFG, n: int = 1) -> None:
    """Обновить исход: joined|left|reported|chat_flood (n штук разом).

    chat_flood — ставит заморозку чата с экспоненциальным backoff по числу
    повторов (чат ответил, что приём заморожен); для него n игнорируется, потому
    что это одно событие чата, а не счётчик участников.
    """
    if n <= 0:
        return
    col = {"joined": "joined_total", "left": "left_total",
           "reported": "reported_total"}.get(outcome)
    try:
        await _ensure(pool, owner_id, chat_key)
        if col:
            await pool.execute(
                f"UPDATE chat_invite_state SET {col} = {col} + $3, updated_at = now() "
                "WHERE owner_id=$1 AND chat_key=$2", owner_id, chat_key, int(n))
        elif outcome == "chat_flood":
            st = await _load(pool, owner_id, chat_key)
            hits = int(st.get("flood_hits") or 0) + 1
            pause = min(cfg.chat_flood_base_pause_sec * (2 ** (hits - 1)),
                        cfg.chat_flood_max_pause_sec)
            await pool.execute(
                "UPDATE chat_invite_state SET flood_hits = $3, "
                "paused_until = now() + make_interval(secs => $4), "
                "pause_reason = $5, updated_at = now() "
                "WHERE owner_id=$1 AND chat_key=$2",
                owner_id, chat_key, hits, float(pause),
                "чат заморозил приём (flood) — пауза с backoff")
    except Exception:
        log_exc_swallow(log, f"smart_invite.note_outcome chat={chat_key} out={outcome}")


async def set_liveness(pool, owner_id: int, chat_key: str, score: float) -> None:
    """Записать оценку живости чата (0..1)."""
    try:
        await _ensure(pool, owner_id, chat_key)
        await pool.execute(
            "UPDATE chat_invite_state SET liveness_score=$3, liveness_checked_at=now(), "
            "updated_at=now() WHERE owner_id=$1 AND chat_key=$2",
            owner_id, chat_key, float(max(0.0, min(1.0, score))))
    except Exception:
        log_exc_swallow(log, f"smart_invite.set_liveness chat={chat_key}")


def liveness_from_signals(participants: int, last_msg_age_days: float | None,
                          recent_msgs: int = 0) -> float:
    """Свести сигналы чата в оценку живости 0..1 (для set_liveness).

    Мёртвый = мало участников И давно нет сообщений. Живой = есть недавняя
    переписка. Порог намеренно строгий: «холодный» приток в тихий чат опасен.
    """
    p = min(1.0, participants / 500.0) if participants > 0 else 0.0
    if last_msg_age_days is None:
        fresh = 0.0
    elif last_msg_age_days <= 1:
        fresh = 1.0
    elif last_msg_age_days <= 7:
        fresh = 0.6
    elif last_msg_age_days <= 30:
        fresh = 0.25
    else:
        fresh = 0.0
    activity = min(1.0, recent_msgs / 50.0)
    # свежесть и активность важнее размера: тихий миллионник всё равно рискован.
    return round(0.25 * p + 0.45 * fresh + 0.30 * activity, 3)


async def assess_and_store_liveness(pool, owner_id: int, chat_key: str,
                                    client, entity) -> float | None:
    """Оценить живость чата через Telethon и записать (best-effort).

    Сигналы: число участников (GetFullChannel) и свежесть последнего сообщения.
    «Мёртвый» чат (тихий, малолюдный) — самый опасный для холодного притока,
    поэтому оценку делаем ДО массового инвайта. Сбой не критичен: без оценки
    governor просто не режет темп по живости.
    """
    try:
        from datetime import datetime, timezone
        participants = 0
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await client(GetFullChannelRequest(entity))
            participants = int(getattr(full.full_chat, "participants_count", 0) or 0)
        except Exception:
            log_exc_swallow(log, f"smart_invite liveness participants chat={chat_key}")

        last_age_days = None
        recent = 0
        try:
            msgs = await client.get_messages(entity, limit=30)
            if msgs:
                now = datetime.now(timezone.utc)
                newest = getattr(msgs[0], "date", None)
                if newest is not None:
                    last_age_days = max(0.0, (now - newest).total_seconds() / 86400.0)
                recent = sum(1 for m in msgs
                             if getattr(m, "date", None) is not None
                             and (now - m.date).total_seconds() < 7 * 86400)
        except Exception:
            log_exc_swallow(log, f"smart_invite liveness messages chat={chat_key}")

        score = liveness_from_signals(participants, last_age_days, recent)
        await set_liveness(pool, owner_id, chat_key, score)
        return score
    except Exception:
        log_exc_swallow(log, f"smart_invite.assess_and_store_liveness chat={chat_key}")
        return None
