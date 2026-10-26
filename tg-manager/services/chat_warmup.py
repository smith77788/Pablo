"""Разогрев ЛЮБОГО чата флотом с ОСМЫСЛЕННЫМ диалогом.

Пользователь указывает чат, выбирает аккаунты флота и режим — флот вступает и
ведёт естественную беседу: между собой по темам и/или реагируя на реальных
участников, понимая контекст. Осмысленность — за счёт LLM (`ai_claude`): каждая
реплика генерируется по СВЕЖЕМУ контексту чата в роли живого человека, а не по
шаблону. Это принципиально отличает модуль от `activity_engine`/`ghost` (там
случайные шаблоны реакций).

Композиция существующих модулей:
  • spintax_ai.complete — генерация реплики по контексту с каскадом провайдеров:
    Claude Opus (ключ/ambient) → бесплатный fallback Groq/OpenRouter/Gemini;
  • account_manager (_make_client / send / join) — чтение и отправка флотом;
  • op_worker.try_claim_account — атомарный захват сессии (без AUTH_KEY_DUPLICATED);
  • fleet_governor / geo_tempo — человеческий темп, тишина под давлением;
  • content_safety — гард исходящего текста;
  • persona_engine (опц.) — характер аккаунта для устойчивого стиля.

Слои: ЧИСТОЕ ядро (plan_turn / build_dialogue_prompt / sanitize_reply) —
юнит-тестируемо; generate_reply — LLM; фоновый loop run() — как ghost_engine.

Режимы (mode):
  • seed   — флот общается ТОЛЬКО между собой по темам (оживить тихий чат);
  • engage — флот РЕАГИРУЕТ на реальных участников (не болтает сам с собой);
  • mixed  — приоритет вовлечения реальных, иначе поддерживает беседу (по умолч.).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from services import account_status as _acc_status

log = logging.getLogger(__name__)

# Потолок одиночного запроса к Telegram в этом фоновом цикле.
#
# Здесь он не «на всякий случай»: цикл держит аккаунт через единый арбитр
# op_worker.try_claim_account, а отпускает его в finally. Повисший запрос
# (мёртвый прокси отдаёт half-open сокет: TCP есть, ответа нет и не будет) из
# finally не возвращается НИКОГДА, поэтому аккаунт остаётся в
# op_worker._accounts_in_use, а renew_leases() продлевает его аренду каждые
# 30 секунд — вечно. Реконсилер такую строку не чистит намеренно: для него
# живая память держателя и есть доказательство занятости. Итог — аккаунт
# навсегда «занят операцией»: он выпадает и из операций владельца, и из
# прогрева, и из призрака, и заметить это можно только по счётчику флота.
#
# 45 секунд — щедро: живой Telegram отвечает за секунды. Занижать нельзя,
# ложный таймаут уводит действие в повтор, то есть в лишний запрос по Telegram.
_TG_TIMEOUT = 45


MODES = ("seed", "engage", "mixed")

# Фиксированная модель Groq для разогрева — сильнее дефолтной, живее диалог.
# Переопределяемо через env, но по умолчанию llama-3.3-70b-versatile.
GROQ_WARMUP_MODEL = os.getenv("CHAT_WARMUP_GROQ_MODEL", "llama-3.3-70b-versatile")

# Паузы между репликами (сек, диапазон для джиттера) — человеческий темп.
INTENSITY = {
    "calm":   (300, 900),    # раз в 5–15 мин
    "normal": (90, 300),     # раз в 1.5–5 мин
    "active": (30, 120),     # раз в 0.5–2 мин
}

# Сколько последних сообщений чата даём LLM как контекст.
CONTEXT_WINDOW = 14
# Максимальная длина реплики (символов) — живые сообщения короткие.
MAX_REPLY_LEN = 320

# Реакции-эмодзи: иногда живой человек не пишет, а ставит реакцию. Лёгкий след,
# добавляет естественности. Набор — стандартные реакции Telegram.
REACTIONS = ("👍", "🔥", "❤️", "😁", "🤔", "👏", "💯", "🙏", "😍", "🤝")
# Вероятность, что ход-ответ реального участника будет РЕАКЦИЕЙ, а не текстом.
REACT_CHANCE = 0.28


def pick_reaction() -> str:
    """Случайная реакция из безопасного набора. ЧИСТАЯ функция."""
    return random.choice(REACTIONS)


def should_react(kind: str, has_target: bool, roll: float | None = None) -> bool:
    """Ставить реакцию вместо текста на этом ходу? Только когда отвечаем реальному
    участнику (kind='reply' + есть цель). ЧИСТАЯ функция (roll — для тестов)."""
    if kind != "reply" or not has_target:
        return False
    r = random.random() if roll is None else roll
    return r < REACT_CHANCE


def valid_mode(mode: str | None) -> str:
    m = (mode or "mixed").strip().lower()
    return m if m in MODES else "mixed"


def intensity_delay(intensity: str | None) -> tuple[int, int]:
    return INTENSITY.get((intensity or "normal").strip().lower(), INTENSITY["normal"])


# ── ЧИСТОЕ ядро: кто говорит и на что отвечает ───────────────────────────────

def _pick_speaker(own_ids: list[int], last_speaker) -> int | None:
    """Следующий говорящий из флота — не тот же, что говорил последним."""
    ids = [int(a) for a in own_ids]
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    pool = [i for i in ids if i != last_speaker] or ids
    return random.choice(pool)


def plan_turn(recent: list[dict], own_ids: list[int], mode: str,
              last_speaker=None, last_seen_msg: int = 0) -> dict | None:
    """Решить следующий ход диалога. ЧИСТАЯ функция (без БД/сети/LLM).

    recent — последние сообщения [{"id","sender_id","sender_name","text","is_fleet"}]
    в хронологическом порядке (старые → новые). own_ids — id аккаунтов флота.
    Возвращает {speaker, kind, reply_to_id, reply_to_text, reply_to_sender} либо
    None (ход пропускаем — например, engage без новых внешних сообщений).
    """
    mode = valid_mode(mode)
    speaker = _pick_speaker(own_ids, last_speaker)
    if speaker is None:
        return None

    # Свежие внешние сообщения (не от флота), которые ещё не обрабатывали.
    # Признак «свой» — флаг is_fleet (исполнитель ставит его по tg_user_id флота);
    # own_ids здесь только для выбора говорящего (это внутренние id аккаунтов).
    external = [m for m in recent
                if not m.get("is_fleet")
                and int(m.get("id") or 0) > int(last_seen_msg or 0)
                and (m.get("text") or "").strip()]
    target = external[-1] if external else None

    if mode == "engage":
        if not target:
            return None  # в engage не разговариваем сами с собой
        kind = "reply"
    elif mode == "seed":
        target = None    # seed игнорирует внешних — только беседа флота по темам
        kind = "seed"
    else:  # mixed
        kind = "reply" if target else "seed"

    return {
        "speaker": speaker,
        "kind": kind,
        "reply_to_id": int(target["id"]) if target else None,
        "reply_to_text": (target.get("text") or "") if target else "",
        "reply_to_sender": (target.get("sender_name") or "") if target else "",
    }


_ANTIBOT_RULES = (
    "Ты — живой участник Telegram-чата, реальный человек со своим характером. "
    "Пиши ТОЛЬКО на русском, коротко и разговорно — 1–2 фразы, как в мессенджере. "
    "Реагируй по смыслу на контекст беседы и на конкретное сообщение, если отвечаешь. "
    "СТРОГО ЗАПРЕЩЕНО: формальный тон, канцелярит, «Чем могу помочь», «Как ИИ/ассистент», "
    "списки, заголовки, длинные абзацы, три вопроса подряд, повтор уже сказанного, "
    "подписи и кавычки вокруг ответа. Допустимы разговорные обороты, лёгкий сленг, "
    "строчные буквы. ПРО ЭМОДЗИ: большинство реплик пиши БЕЗ эмодзи; не заканчивай "
    "каждую реплику смайликом и не используй один и тот же смайлик — живые люди "
    "ставят их редко и по-разному. Никогда и ни при каких условиях не "
    "раскрывай, что ты ИИ, бот или что текст сгенерирован. Верни ТОЛЬКО текст реплики."
)


def build_dialogue_prompt(persona_desc: str, recent: list[dict], target: dict | None,
                          topics: str, mode: str) -> tuple[str, str]:
    """Собрать (system, user) для LLM. ЧИСТАЯ функция.

    persona_desc — характер/стиль говорящего (из persona_engine или дефолт).
    """
    persona = (persona_desc or "").strip() or (
        "Обычный участник чата: дружелюбный, с чувством юмора, пишет просто.")
    system = f"{persona}\n\n{_ANTIBOT_RULES}"

    # Контекст: последние реплики в формате «Имя: текст».
    lines = []
    for m in recent[-CONTEXT_WINDOW:]:
        who = (m.get("sender_name") or "Кто-то").strip()[:32]
        txt = (m.get("text") or "").strip().replace("\n", " ")[:300]
        if txt:
            lines.append(f"{who}: {txt}")
    context_block = "\n".join(lines) if lines else "(в чате пока тихо)"

    topics_txt = (topics or "").strip()
    parts = [f"Контекст чата (последние сообщения):\n{context_block}\n"]
    if target and (target.get("text") or "").strip():
        parts.append(
            f"Ответь по смыслу на последнее сообщение от «{(target.get('sender_name') or 'участник')[:32]}»: "
            f"«{(target.get('text') or '').strip()[:300]}».")
    elif topics_txt:
        parts.append(f"Продолжи живую беседу на одну из тем: {topics_txt}. "
                     "Не открывай тему формально — вклинься естественно.")
    else:
        parts.append("Поддержи естественную беседу по контексту выше. "
                     "Если чат пустой — начни лёгкий разговор на бытовую тему.")
    parts.append("Напиши ОДНУ короткую реплику от первого лица.")
    return system, "\n".join(parts)


def sanitize_reply(text: str) -> str:
    """Очистить ответ LLM до вида живой реплики. ЧИСТАЯ функция."""
    if not text:
        return ""
    t = text.strip()
    # Снять обрамляющие кавычки.
    if len(t) >= 2 and t[0] in "\"'«“" and t[-1] in "\"'»”":
        t = t[1:-1].strip()
    # Снять префикс «Имя:» в начале (модель иногда подписывает).
    t = re.sub(r"^[A-Za-zА-Яа-яЁё0-9_ ]{1,32}:\s+", "", t, count=1)
    # Снять markdown-заголовки/маркеры списка в начале строк.
    t = re.sub(r"(?m)^\s*[#>\-\*]+\s*", "", t)
    # Схлопнуть переносы — живая реплика однострочна.
    t = re.sub(r"\s*\n\s*", " ", t).strip()
    # Выкинуть явные само-раскрытия ИИ (страховка поверх промпта).
    low = t.lower()
    if any(p in low for p in ("как ии", "как ассистент", "я — ии", "я ии",
                              "языковая модель", "as an ai", "я бот", "как бот")):
        return ""
    if len(t) > MAX_REPLY_LEN:
        # Обрезать по границе предложения, не рвать слово.
        cut = t[:MAX_REPLY_LEN]
        m = re.search(r"[.!?…]\s", cut[::-1])
        t = cut[: MAX_REPLY_LEN - m.start()].strip() if m else cut.rsplit(" ", 1)[0].strip()
    return t.strip()


# ── LLM-генерация реплики ────────────────────────────────────────────────────

async def generate_reply(persona_desc: str, recent: list[dict], target: dict | None,
                         topics: str, mode: str) -> str | None:
    """Сгенерировать осмысленную реплику по контексту. None — если LLM недоступен
    или ответ пустой/не прошёл очистку (лучше пропустить ход, чем слать мусор:
    осмысленность важнее активности).

    Каскад: сначала Claude (ключ/ambient), затем ФИКСИРОВАННАЯ Groq-модель
    llama-3.3-70b-versatile (сильнее дефолтной — живее диалог), затем прочие
    OpenAI-совместимые провайдеры (OpenRouter/Gemini) через spintax_ai. Так
    разогрев работает даже без ключа Anthropic — достаточно бесплатного Groq."""
    text, _ = await generate_reply_detailed(persona_desc, recent, target, topics, mode)
    return text


async def generate_reply_detailed(persona_desc: str, recent: list[dict],
                                  target: dict | None, topics: str,
                                  mode: str) -> tuple[str | None, str]:
    """То же самое, но вместе с ПРИЧИНОЙ, если реплики нет.

    Зачем отдельная функция. Каскад провайдеров глотал ошибки каждого: ключ
    неверный, квота кончилась, провайдер не отвечает — снаружи всё это
    выглядело одинаково («реплики нет»), и разогрев молчал без объяснений. А
    действия у владельца разные: неверный ключ надо менять, перегрузку —
    переждать. Возвращаем (текст|None, причина по-русски); причина пустая,
    когда текст есть.

    Контракт `generate_reply` не меняется: она тонкая обёртка над этой.
    """
    errors: list[str] = []
    try:
        from services import ai_claude, spintax_ai
        from services.ai_providers import configured_providers
        # Нет ни Claude, ни настроенных провайдеров → писать нечем.
        if not (ai_claude.enabled() or configured_providers()):
            return None, _no_text_reason()
        system, user = build_dialogue_prompt(persona_desc, recent, target, topics, mode)
        raw = None
        # 1) Claude Opus — предпочтительный путь.
        if ai_claude.enabled():
            try:
                raw = await ai_claude.complete(system, user, timeout=60.0)
            except Exception as e:
                raw = None
                errors.append(f"Claude: {e}")
        # 2) Groq на зафиксированной llama-3.3-70b.
        if not (raw and raw.strip()):
            raw, _err = await _groq_complete_detailed(system, user)
            if _err:
                errors.append(f"Groq: {_err}")
        # 3) Остальные провайдеры (OpenRouter/Gemini) — общий каскад spintax_ai.
        if not (raw and raw.strip()):
            try:
                raw = await spintax_ai.complete(system, user)
            except Exception as e:
                raw = None
                errors.append(f"Остальные провайдеры: {e}")
        clean = sanitize_reply(raw or "") or None
        if clean:
            return clean, ""
        if errors:
            return None, "Провайдеры ответили ошибкой — " + "; ".join(errors)[:200]
        if raw and raw.strip():
            # Текст был, но очистка его забраковала: самораскрытие ИИ или мусор.
            return None, "Ответ модели не прошёл очистку — ход пропущен"
        return None, _no_text_reason()
    except Exception as e:
        log.debug("chat_warmup.generate_reply failed: %s", e)
        return None, f"Генерация сорвалась: {e}"


def _no_text_reason() -> str:
    """Почему реплики нет: совсем нечем писать или модель не ответила сейчас."""
    try:
        from services import ai_claude
        from services.ai_providers import configured_providers

        if ai_claude.enabled() or configured_providers():
            return ("Модель не вернула текст — ход пропущен "
                    "(обычно временно: перегрузка или таймаут провайдера)")
    except Exception:
        return "Текст не сгенерирован: генератор ответов недоступен"
    return ("Не задан ни один ключ ИИ — флот вступил в чат, но писать ему нечем. "
            "Задайте ключ (Claude или Groq) в админ-меню «Ключи ИИ»")


async def _groq_complete(system: str, user: str, timeout: float = 45.0) -> str | None:
    """Прямой вызов Groq на ФИКСИРОВАННОЙ модели GROQ_WARMUP_MODEL. None — если
    Groq не настроен или запрос не удался (тогда вызывающий идёт дальше по каскаду)."""
    text, _ = await _groq_complete_detailed(system, user, timeout)
    return text


async def _groq_complete_detailed(system: str, user: str,
                                  timeout: float = 45.0) -> tuple[str | None, str]:
    """То же, но вместе с текстом ошибки — её имя владелец и увидит в журнале."""
    try:
        from services.ai_providers import configured_providers
        prov = next((p for p in configured_providers() if getattr(p, "name", "") == "groq"), None)
        if not prov:
            return None, ""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=prov.api_key, base_url=prov.base_url, timeout=timeout)
        resp = await client.chat.completions.create(
            model=GROQ_WARMUP_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=300, temperature=0.9)   # живее и короче — как в чате
        txt = (resp.choices[0].message.content or "").strip()
        return (txt or None), ("" if txt else "пустой ответ модели")
    except Exception as e:
        log.debug("chat_warmup._groq_complete failed: %s", e)
        return None, str(e)[:160]


# ── CRUD сессий ──────────────────────────────────────────────────────────────

async def create_session(pool, owner_id: int, chat_ref: str, account_ids: list[int],
                         mode: str = "mixed", topics: str = "",
                         intensity: str = "normal") -> dict:
    row = await pool.fetchrow(
        """INSERT INTO chat_warmup_sessions
               (owner_id, chat_ref, account_ids, mode, topics, intensity, status)
           VALUES ($1,$2,$3,$4,$5,$6,'active')
           RETURNING *""",
        owner_id, chat_ref.strip(), [int(a) for a in account_ids],
        valid_mode(mode), (topics or "").strip(),
        (intensity or "normal").strip().lower())
    return dict(row)


async def list_sessions(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM chat_warmup_sessions WHERE owner_id=$1 ORDER BY created_at DESC",
        owner_id)
    return [dict(r) for r in rows]


async def get_session(pool, owner_id: int, sid: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM chat_warmup_sessions WHERE id=$1 AND owner_id=$2", sid, owner_id)
    return dict(row) if row else None


async def set_status(pool, owner_id: int, sid: int, status: str) -> bool:
    if status not in ("active", "paused", "stopped"):
        return False
    res = await pool.execute(
        "UPDATE chat_warmup_sessions SET status=$3, updated_at=NOW() "
        "WHERE id=$1 AND owner_id=$2", sid, owner_id, status)
    return res != "UPDATE 0"


async def _touch(pool, sid: int, speaker: int, last_seen_msg: int, sent: bool) -> None:
    await pool.execute(
        "UPDATE chat_warmup_sessions SET last_run_at=NOW(), last_speaker=$2, "
        "last_seen_msg=GREATEST(last_seen_msg,$3), "
        "messages_sent=messages_sent + $4, updated_at=NOW() WHERE id=$1",
        sid, int(speaker or 0), int(last_seen_msg or 0), 1 if sent else 0)


# ── Фоновый исполнитель: один осмысленный ход диалога ────────────────────────

# ── Журнал ходов: что флот написал и почему промолчал ────────────────────────
#
# У разогрева были только счётчики сессии, поэтому владелец видел строку
# «активен · смешанный · отправлено 0» и не мог узнать ровным счётом ничего.
# А промолчать ход может по десятку причин — аккаунт на кулдауне, в карантине,
# занят операцией, флот не смог войти в чат, ночное окно, генератор ответов
# недоступен, текст отклонён проверкой контента, Telegram попросил паузу — и ни
# одна из них нигде не оставалась. Для массовой операции по живым аккаунтам это
# прямое нарушение гейта продукта: per-target лог обязателен.

#: Сколько символов реплики хранить в журнале: экран показывает начало, а не всё.
_LOG_TEXT_MAX = 500


def _excerpt(sender, text) -> str:
    """«Кому отвечали» одной строкой: имя и начало сообщения."""
    name = str(sender or "участник").strip()
    body = " ".join(str(text or "").split())[:90]
    return f"{name}: {body}" if body else name


async def _log(pool, session: dict, status: str, reason: str = "", *,
               account_id: int | None = None, kind: str = "turn",
               text: str = "", reply_to: str = "") -> None:
    """Записать ход в журнал. Никогда не бросает: журнал не имеет права сорвать ход.

    Подряд идущие пропуски с одной причиной не плодят строки — у последней
    растёт `repeats` и обновляется `created_at`. Сессия, которая сутки не может
    войти в чат, иначе написала бы тысячи одинаковых строк, а владельцу нужно
    одно: что мешает прямо сейчас и давно ли.
    """
    try:
        sid = int(session["id"])
        owner_id = int(session["owner_id"])
        reason = (reason or "")[:300]
        if status != "ok":
            prev = await pool.fetchrow(
                "SELECT id, status, COALESCE(reason,'') AS reason FROM chat_warmup_log "
                " WHERE session_id=$1 ORDER BY id DESC LIMIT 1", sid)
            if prev and prev["status"] == status and prev["reason"] == reason:
                await pool.execute(
                    "UPDATE chat_warmup_log SET repeats = repeats + 1, created_at = NOW() "
                    " WHERE id=$1", int(prev["id"]))
                return
        await pool.execute(
            """INSERT INTO chat_warmup_log
                   (session_id, owner_id, account_id, kind, status, reason, text, reply_to)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            sid, owner_id,
            int(account_id) if account_id else None,
            kind, status, reason or None,
            (text or "")[:_LOG_TEXT_MAX] or None,
            (reply_to or "")[:200] or None,
        )
    except Exception as exc:
        log.debug("chat_warmup: журнал хода не записан: %s", exc)


async def diagnose(pool, owner_id: int, session: dict) -> dict:
    """Почему сессия молчит ПРЯМО СЕЙЧАС — по тем же гейтам, что и ход.

    Журнал отвечает на тот же вопрос, но только после того, как ход случился.
    Владельцу, у которого разогрев «активен, а флот молчит», ответ нужен в ту
    секунду, когда он открыл экран, — и все данные для него уже лежат в базе:
    статус аккаунта, пауза Telegram, карантин по здоровью, захват под операцию,
    наличие ключа ИИ. Проверяем ровно те условия, по которым ход отказывается
    говорить, и в том же порядке.

    Возвращает {"ready": N, "accounts": [{label, ok, reason}], "global": [причины]}.
    Никогда не бросает: диагностика не имеет права сломать экран.
    """
    out: dict = {"ready": 0, "accounts": [], "global": []}
    try:
        ids = [int(a) for a in (session.get("account_ids") or [])]
        if not ids:
            out["global"].append("В сессии не выбрано ни одного аккаунта флота")
            return out

        rows = await pool.fetch(
            "SELECT a.id, "
            "       COALESCE(a.username, a.first_name, a.phone, '#' || a.id::text) AS label, "
            "       a.is_active, a.session_str IS NOT NULL AS has_session, "
            "       COALESCE(a.acc_status, 'active') AS acc_status, "
            "       a.cooldown_until, a.in_operation "
            "  FROM tg_accounts a WHERE a.id = ANY($1::bigint[]) AND a.owner_id=$2",
            ids, int(owner_id))
        seen = {int(r["id"]): r for r in rows}

        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        for acc_id in ids:
            r = seen.get(acc_id)
            if r is None:
                out["accounts"].append({"label": f"#{acc_id}", "ok": False,
                                        "reason": "аккаунт удалён или сменил владельца"})
                continue
            label = r["label"]
            reason = ""
            if not r["is_active"]:
                reason = "выключен владельцем"
            elif not r["has_session"]:
                reason = "нет строки сессии — перезалейте аккаунт"
            elif _acc_status.is_dead(r["acc_status"]):
                reason = f"статус «{r['acc_status']}» — для действий не годится"
            elif r["cooldown_until"] and r["cooldown_until"] > now:
                _left = int((r["cooldown_until"] - now).total_seconds() // 60)
                reason = f"пауза Telegram ещё {max(1, _left)} мин"
            elif r["in_operation"]:
                reason = "занят операцией прямо сейчас"
            else:
                try:
                    from services.infra_memory import is_account_quarantined

                    if await is_account_quarantined(pool, acc_id):
                        reason = "в карантине по здоровью"
                except Exception:
                    pass  # пульс недоступен — считаем аккаунт годным (fail-open)
            out["accounts"].append({"label": label, "ok": not reason,
                                    "reason": reason})
            if not reason:
                out["ready"] += 1

        if not out["ready"]:
            out["global"].append(
                "Ни один аккаунт сессии сейчас не может сделать ход")
        try:
            from services import ai_claude
            from services.ai_providers import configured_providers

            if not (ai_claude.enabled() or configured_providers()):
                out["global"].append(
                    "Не задан ни один ключ ИИ: разогрев пишет только тексты, "
                    "которые сочиняет модель")
        except Exception:
            log.debug("chat_warmup.diagnose: статус ИИ недоступен", exc_info=True)
    except Exception as exc:
        log.debug("chat_warmup.diagnose: %s", exc)
    return out


async def _defer(pool, sid: int) -> None:
    """Отложить сессию до следующего интервала, не трогая счётчики.

    ЧТО ЛОМАЛОСЬ. `last_run_at` обновлялся ТОЛЬКО в конце хода, дошедшего до
    самого низа (`_touch`). Любой ранний выход — аккаунт на кулдауне, карантин,
    занят операцией, не вошли в чат, нет новых сообщений — оставлял отметку
    нетронутой, а `_due` без неё считает сессию созревшей ВСЕГДА. Цикл ходит раз
    в 20 секунд, значит сессия, которая не может сделать ход, повторяла попытку
    каждые 20 секунд круглые сутки: открывала сессию Telegram, вступала в чат,
    читала историю — и снова упиралась в ту же причину. Это не только пустая
    нагрузка, это прямой риск бана: такой частоты у живого участника не бывает.

    Теперь пропуск стоит столько же, сколько ход: следующая попытка — по
    интенсивности сессии (1.5–5 минут на обычной), как и задумано.
    """
    try:
        await pool.execute(
            "UPDATE chat_warmup_sessions SET last_run_at=NOW(), updated_at=NOW() "
            " WHERE id=$1", int(sid))
    except Exception as exc:
        log.debug("chat_warmup: отложить сессию %s не удалось: %s", sid, exc)


async def _skip(pool, session: dict, reason: str, *, status: str = "skipped",
                **kw) -> None:
    """Пропустить ход: назвать причину в журнале и отложить до следующего раза."""
    await _log(pool, session, status, reason, **kw)
    await _defer(pool, int(session["id"]))


async def _warmup_allowed(pool, owner_id: int, geo_country, now) -> bool:
    """Разрешить реплику сейчас? Уважает губернатор (реже под давлением) и
    локальную ночь аккаунта. Fail-open. Только skip-гейт (сессий не открывает)."""
    try:
        level = "green"
        if owner_id:
            from services import fleet_governor
            mult = await fleet_governor.tempo_multiplier(pool, owner_id)
            level = fleet_governor.level_for_multiplier(mult)
        night = False
        try:
            from services import geo_tempo
            night = geo_tempo.is_local_night(geo_country, now)
        except Exception:
            night = False
        skip = {"green": 0.0, "yellow": 0.25, "orange": 0.55, "red": 0.85}.get(level, 0.0)
        if night:
            skip = max(skip, 0.7)
        return random.random() >= skip
    except Exception:
        return True


async def _fleet_tg_ids(pool, account_ids: list[int]) -> set[int]:
    try:
        rows = await pool.fetch(
            "SELECT tg_user_id FROM tg_accounts WHERE id = ANY($1::bigint[]) "
            "AND tg_user_id IS NOT NULL", [int(a) for a in account_ids])
        return {int(r["tg_user_id"]) for r in rows}
    except Exception:
        return set()


def _sender_name(msg) -> str:
    s = getattr(msg, "sender", None)
    if s is not None:
        for attr in ("first_name", "username", "title"):
            v = getattr(s, attr, None)
            if v:
                return str(v)
    return "Участник"


def _msg_to_dict(msg, own_tg_ids: set[int]) -> dict:
    sid = int(getattr(msg, "sender_id", 0) or 0)
    return {
        "id": int(getattr(msg, "id", 0) or 0),
        "sender_id": sid,
        "sender_name": _sender_name(msg),
        "text": (getattr(msg, "message", None) or getattr(msg, "text", None) or ""),
        "is_fleet": sid in own_tg_ids,
    }


async def _resolve_and_join(client, chat_ref: str):
    """Вернуть entity целевого чата, лениво вступив при необходимости (в ТОМ ЖЕ
    клиенте — без второго параллельного коннекта)."""
    ref = (chat_ref or "").strip()
    # Приватная инвайт-ссылка: t.me/+hash или /joinchat/hash.
    m = re.search(r"(?:joinchat/|/\+)([\w-]+)", ref)
    if m:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.errors import UserAlreadyParticipantError
        try:
            upd = await asyncio.wait_for(client(ImportChatInviteRequest(m.group(1))), timeout=_TG_TIMEOUT)
            return upd.chats[0]
        except UserAlreadyParticipantError:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            inv = await asyncio.wait_for(client(CheckChatInviteRequest(m.group(1))), timeout=_TG_TIMEOUT)
            return getattr(inv, "chat", None)
    # Публичный @username / ссылка / id.
    entity = await asyncio.wait_for(client.get_entity(ref), timeout=_TG_TIMEOUT)
    from telethon.tl.functions.channels import JoinChannelRequest
    try:
        await asyncio.wait_for(client(JoinChannelRequest(entity)), timeout=_TG_TIMEOUT)
    except Exception as e:
        # Здесь стоял голый `pass`: уже участник, базовая группа или нет прав —
        # всё это действительно не мешает читать и писать. Но тем же `pass`
        # глоталась и ПАУЗА Telegram, после чего ход продолжался: аккаунт читал
        # историю, ставил реакцию и писал сообщение под действующим
        # ограничением. Так зарабатывают спамблок, а не прогрев.
        _name = type(e).__name__
        if "flood" in _name.lower() or _name in (
                "UserChannelsTooMuchError", "ChannelsTooMuchError"):
            raise
    return entity


# Разнообразные дефолт-персоны: когда у аккаунта нет своей персоны (persona_engine
# пуст), НЕЛЬЗЯ давать всем одинаковый характер — иначе весь флот говорит одним
# голосом и одинаково лепит смайлик в конце (заметный шаблон, риск бана). Выбираем
# устойчиво по id аккаунта, чтобы у каждого был свой стабильный, но ОТЛИЧНЫЙ стиль;
# часть персон принципиально без эмодзи и разной длины/тона.
_DEFAULT_PERSONAS = (
    "Немногословный практик: пишешь коротко и по делу, без эмодзи, иногда с лёгкой иронией.",
    "Скептик: сомневаешься, переспрашиваешь, любишь уточнять детали. Эмодзи почти не ставишь.",
    "Тёплый общительный человек: поддерживаешь беседу, но без наигранности; смайлик — редко и к месту.",
    "Сухой технарь: факты, конкретика, минимум эмоций и без смайликов.",
    "Ироничный балагур: шутишь, но не в каждой фразе; эмодзи используешь изредка и разные.",
    "Спокойный наблюдатель: короткие реплики, чаще соглашаешься или уточняешь, эмодзи не любишь.",
    "Живой и эмоциональный, но пишешь строчными и небрежно, эмодзи ставишь по настроению, не всегда.",
    "Прагматик с житейским опытом: делишься мнением просто, без смайликов, иногда с сарказмом.",
)


def _fallback_persona(account_id: int) -> str:
    """Устойчивый разнообразный характер по id — чтобы флот не говорил одинаково."""
    return _DEFAULT_PERSONAS[int(account_id) % len(_DEFAULT_PERSONAS)]


async def _persona_desc(pool, account_id: int) -> str:
    try:
        from services import persona_engine
        p = await persona_engine.get_persona(pool, account_id)
        if p:
            return persona_engine.build_persona_system_prompt(p)
    except Exception:
        pass
    # Нет своей персоны → не пустая строка (иначе единый дефолт на весь флот), а
    # разнообразный характер, привязанный к аккаунту.
    return _fallback_persona(account_id)


async def _process_session(pool, session: dict) -> None:
    from services import account_manager
    sid = int(session["id"])
    owner_id = int(session["owner_id"])
    account_ids = [int(a) for a in (session.get("account_ids") or [])]
    mode = valid_mode(session.get("mode"))
    if not account_ids:
        await _skip(pool, session, "В сессии не выбрано ни одного аккаунта флота")
        return
    speaker = _pick_speaker(account_ids, session.get("last_speaker"))
    if speaker is None:
        await _skip(pool, session,
                    "Некому говорить: подходящий аккаунт не выбрался")
        return

    acc = await pool.fetchrow(
        "SELECT a.id, a.session_str, a.device_model, a.system_version, a.app_version, "
        "a.lang_code, a.system_lang_code, a.proxy_id, p.geo_country "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.id=$1 AND a.is_active AND a.session_str IS NOT NULL "
        "AND " + _acc_status.sql_not_dead("a.acc_status") + " "
        # Аккаунт на паузе Telegram трогать нельзя: прогрев — такое же живое
        # действие, как операция, и ход под действующим ограничением приближает
        # спамблок вместо того, чтобы его отдалить.
        "AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())",
        speaker)
    if not acc:
        await _skip(pool, session,
                    "Аккаунт недоступен: выключен, без сессии, мёртв или на паузе Telegram",
                    account_id=speaker)
        return
    # Единый пульс здоровья флота, fail-open: недавнее серьёзное ограничение —
    # аккаунт отдыхает, а не прогревается.
    try:
        from services.infra_memory import is_account_quarantined

        if await is_account_quarantined(pool, int(speaker)):
            await _skip(pool, session,
                        "Аккаунт в карантине по здоровью — ход пропущен",
                        account_id=speaker)
            return
    except Exception:
        log.debug("chat_warmup: пульс здоровья недоступен, продолжаем (fail-open)")
    if not await _warmup_allowed(pool, owner_id, acc.get("geo_country"), __import__("datetime").datetime.now(__import__("datetime").timezone.utc)):
        await _skip(pool, session,
                    "Темп снижен: губернатор флота или ночь в регионе аккаунта",
                    account_id=speaker)
        return

    # Атомарный захват — одна сессия не коннектится диалогом И операцией/прогревом.
    try:
        from services import op_worker
        leased = await op_worker.try_claim_account(int(speaker))
    except Exception:
        op_worker = None  # type: ignore
        leased = True
    if not leased:
        await _skip(pool, session,
                    "Аккаунт занят другой операцией — ход пропущен",
                    account_id=speaker)
        return

    # Пост-захватный путь под одним try с release в finally. Раньше _fleet_tg_ids
    # (запрос в БД) и _make_client стояли ВНЕ try — их сбой (ошибка БД, битая
    # сессия) уводил аккаунт мимо release: он залипал «занятым» в _accounts_in_use
    # до рестарта процесса, а аренда продлевалась вечно (корень «весь флот занят
    # без операций»).
    client = None
    sent = False
    max_seen = int(session.get("last_seen_msg") or 0)
    try:
        own_tg = await _fleet_tg_ids(pool, account_ids)
        acc_d = dict(acc)
        client = account_manager._make_client(acc_d["session_str"], acc_d)
        await account_manager._connect_and_track(client, acc_d, "chat_warmup")
        entity = await _resolve_and_join(client, session["chat_ref"])
        if entity is None:
            # Самая частая и самая непонятная снаружи причина: флот физически не
            # в чате, поэтому «активен · отправлено 0» держится вечно.
            await _skip(pool, session,
                        "Не удалось войти в чат: проверьте ссылку и доступ "
                        "(закрытый чат, бан аккаунта, неверный адрес)",
                        status="error", account_id=speaker)
            return
        raw = await asyncio.wait_for(client.get_messages(entity, limit=CONTEXT_WINDOW), timeout=_TG_TIMEOUT)
        recent = [_msg_to_dict(m, own_tg) for m in reversed(list(raw))]  # старые → новые
        plan = plan_turn(recent, account_ids, mode, session.get("last_speaker"),
                         int(session.get("last_seen_msg") or 0))
        if recent:
            max_seen = max([max_seen] + [m["id"] for m in recent if not m["is_fleet"]])
        if not plan:
            await _skip(pool, session,
                        "Новых сообщений в чате нет — ждём участников",
                        account_id=speaker)
            return

        if should_react(plan["kind"], bool(plan.get("reply_to_id"))):
            # Иногда живой человек просто ставит реакцию, а не пишет — лёгкий след.
            try:
                from telethon.tl.functions.messages import SendReactionRequest
                from telethon.tl.types import ReactionEmoji
                _reaction = pick_reaction()
                await asyncio.wait_for(client(SendReactionRequest(
                    peer=entity, msg_id=int(plan["reply_to_id"]),
                    reaction=[ReactionEmoji(emoticon=_reaction)])), timeout=_TG_TIMEOUT)
                sent = True
                await _log(pool, session, "ok", kind="reaction", text=_reaction,
                           account_id=speaker,
                           reply_to=_excerpt(plan.get("reply_to_sender"),
                                             plan.get("reply_to_text")))
            except Exception as e:
                log.debug("chat_warmup s=%d: реакция не удалась: %s", sid, e)
                await _log(pool, session, "error",
                           f"Реакция не поставлена: {e}", account_id=speaker,
                           kind="reaction")
        else:
            target = None
            if plan["kind"] == "reply" and plan.get("reply_to_id"):
                target = {"text": plan["reply_to_text"], "sender_name": plan["reply_to_sender"]}
            persona_desc = await _persona_desc(pool, speaker)
            reply, _why = await generate_reply_detailed(
                persona_desc, recent, target, session.get("topics") or "", mode)
            if reply:
                from services import content_safety
                if content_safety.scan_text(reply).blocked:
                    log.info("chat_warmup s=%d: реплика отклонена content_safety", sid)
                    await _log(pool, session, "skipped",
                               "Ответ отклонён проверкой контента — ход пропущен",
                               account_id=speaker, kind=plan["kind"])
                else:
                    import asyncio as _a
                    await _a.sleep(random.uniform(2.0, 6.0))  # имитация набора
                    reply_to = plan["reply_to_id"] if plan["kind"] == "reply" else None
                    await asyncio.wait_for(client.send_message(entity, reply, reply_to=reply_to), timeout=_TG_TIMEOUT)
                    sent = True
                    await _log(pool, session, "ok", kind=plan["kind"], text=reply,
                               account_id=speaker,
                               reply_to=_excerpt(plan.get("reply_to_sender"),
                                                 plan.get("reply_to_text")))
            else:
                # reply пусто — мусора в чат не шлём, но молчать об этом нельзя:
                # снаружи это выглядит как мёртвый разогрев, и ровно так оно и
                # выглядело. Две причины разные, и действия у владельца разные:
                # ключа ИИ нет вовсе (писать нечем — задать ключ) против того,
                # что модель не ответила на этот запрос (само пройдёт).
                await _log(pool, session, "skipped", _why or _no_text_reason(),
                           account_id=speaker, kind=plan["kind"])
    except Exception as e:
        # Пауза Telegram — не рядовой сбой хода: о ней обязан узнать весь
        # продукт, иначе следующая подсистема возьмёт этот аккаунт как
        # спокойный и уведёт под то же ограничение.
        _flood_s = 0
        try:
            from services import flood_engine as _fe

            _secs = _fe.flood_seconds(e) or 0
            if _secs > 0:
                _flood_s = int(_secs)
                await _fe.record_flood(pool, int(speaker), _secs, "chat_warmup")
                log.warning(
                    "chat_warmup s=%d acc=%s: Telegram просит паузу %d с",
                    sid, speaker, _secs)
        except Exception:
            log.warning("chat_warmup: пауза не записана в пульс здоровья",
                        exc_info=True)
        log.debug("chat_warmup s=%d acc=%s ход не удался: %s", sid, speaker, e)
        # Пауза Telegram — не рядовой сбой, и владельцу её надо назвать своими
        # словами: это не поломка разогрева, а требование платформы переждать.
        await _log(
            pool, session, "error",
            (f"Telegram попросил паузу: {max(1, _flood_s // 60)} мин"
             if _flood_s > 0 else f"Ход не удался: {e}"),
            account_id=speaker)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if op_worker is not None:
            try:
                await op_worker.release_accounts([int(speaker)])
            except Exception:
                pass
    await _touch(pool, sid, speaker, max_seen, sent)


def _due(session: dict, now) -> bool:
    """Пора ли сессии сделать ход — по интенсивности с джиттером."""
    last = session.get("last_run_at")
    if last is None:
        return True
    lo, hi = intensity_delay(session.get("intensity"))
    gap = (now - last).total_seconds()
    return gap >= random.uniform(lo, hi)


async def run(pool, bot=None) -> None:
    """Фоновый цикл разогрева чатов. Регистрируется в main как ghost_engine.run.

    Каждый тик обрабатывает по ОДНОМУ ходу на созревшую активную сессию —
    человеческий темп, атомарный захват, губернатор. Осмысленность — LLM."""
    # asyncio теперь импортирован на уровне модуля (потолки запросов), локальный
    # дубль сделал бы имя локальным на всю функцию.
    from datetime import datetime, timezone
    log.info("Chat Warmup engine started")
    while True:
        try:
            rows = await pool.fetch(
                "SELECT * FROM chat_warmup_sessions WHERE status='active' ORDER BY last_run_at NULLS FIRST")
            now = datetime.now(timezone.utc)
            for r in rows:
                s = dict(r)
                if not _due(s, now):
                    continue
                try:
                    await _process_session(pool, s)
                except Exception as e:
                    log.debug("chat_warmup session %s error: %s", s.get("id"), e)
                await asyncio.sleep(random.uniform(1.5, 4.0))  # разнос ходов между сессиями
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("chat_warmup loop error: %s", e)
        await asyncio.sleep(20)
