"""Защита аккаунтов (Account Shield) — проактивная защита от бана.

Каждые 30 минут щит пересчитывает риск флота владельца (`physics_engine`,
модель по факторам) и по ПОЛИТИКЕ владельца решает:
  ok     — ничего не делать;
  warn   — записать предупреждение (без изменений аккаунта);
  cool   — охладить: отодвинуть `cooldown_until` на `cool_hours`;
  pause  — снять с работы на `cool_duration_hours` с меткой
           `shield_paused_until`; по её истечении щит сам вернёт аккаунт;
  resume — пауза щита закончилась (или владелец вернул аккаунт сам).

Политика: профиль (осторожный / сбалансированный / смелый / свой), пороги
охлаждения и паузы, реакция (только предупреждать / охлаждать / охлаждать и
ставить на паузу) и аккаунты-исключения, которых щит не трогает.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import asyncpg

from services import physics_engine

log = logging.getLogger(__name__)


def _apply_failed(action: str, account_id: int, exc: BaseException) -> None:
    """Защита не применилась. Громко и в счётчик, а не `log.debug`.

    `apply_shield_decision` — это и есть защита: «cool» продлевает
    `cooldown_until`, «pause» снимает `is_active`. Сбой записи оставляет
    аккаунт, признанный опасным, полностью активным — он пойдёт в следующую
    массовую операцию в полном темпе. Раньше такой сбой уходил в `log.debug`,
    которого никто не читает, а владельцу при этом отправлялось «Аккаунт
    поставлен на паузу».
    """
    log.error(
        "account_shield: решение «%s» НЕ применилось к acc=%d (%s) — аккаунт "
        "остаётся активным, хотя признан опасным",
        action, account_id, exc,
    )
    try:
        from services import metrics as _m

        _m.inc("infragram_shield_apply_write_failures_total", {"action": action})
    except Exception:
        pass


def _note_write_cancelled(where: str, ctx: str, lost: str) -> None:
    """Отмена съела защитную запись. Разбор и счётчик — у общей двери.

    Писатели защиты ловят `except Exception`, а `asyncio.CancelledError` с
    Python 3.8 не `Exception`: отмена проходила сквозь них без следа, и слепы
    они оказывались ровно к нашим собственным остановкам (деплой, передача
    очереди). Отмену вызывающий обязан пробросить сразу после этого вызова.
    """
    try:
        from services import metrics as _m

        _m.note_write_cancelled(__name__, where, ctx, lost)
    except Exception:
        pass

_LOOP_INTERVAL = 1800  # 30 minutes


# ─── Config dataclass ─────────────────────────────────────────────────────────

# Реакция щита на опасный аккаунт. До v323 был один флажок auto_pause, и при
# выключенном флажке решение «pause» молча не применялось, а в историю и в
# Telegram уходило «Убран из работы». Реакция теперь говорит прямо, сколько
# щиту позволено.
RESPONSE_MODES = ("warn", "cool", "pause")

# Профили — наборы порогов одним касанием. «balanced» совпадает с прежними
# значениями по умолчанию, поэтому у владельцев без настроек ничего не меняется.
PRESETS: dict[str, dict] = {
    "careful": {
        "risk_threshold": 0.5, "ban_prob_threshold": 0.35,
        "pause_risk_threshold": 0.75, "pause_ban_threshold": 0.6,
        "cool_hours": 4, "cool_duration_hours": 48,
    },
    "balanced": {
        "risk_threshold": 0.7, "ban_prob_threshold": 0.5,
        "pause_risk_threshold": 0.9, "pause_ban_threshold": 0.8,
        "cool_hours": 2, "cool_duration_hours": 24,
    },
    "bold": {
        "risk_threshold": 0.8, "ban_prob_threshold": 0.65,
        "pause_risk_threshold": 0.95, "pause_ban_threshold": 0.9,
        "cool_hours": 1, "cool_duration_hours": 12,
    },
}

# Предупреждение — заранее, на этой доле порога охлаждения.
WARN_RATIO = 0.7

# Одинаковое решение по одному аккаунту не пишется в историю чаще, чем раз в
# столько часов: раньше «Предупреждение» по одному аккаунту ложилось каждые
# 30 минут и вытесняло из истории всё остальное.
_REPEAT_QUIET_HOURS = 6


@dataclass
class ShieldConfig:
    risk_threshold: float = 0.7
    ban_prob_threshold: float = 0.5
    auto_pause: bool = True
    notify_admin: bool = True
    cool_duration_hours: int = 24
    pause_risk_threshold: float = 0.9
    pause_ban_threshold: float = 0.8
    cool_hours: int = 2
    response_mode: str = "pause"
    preset: str = "balanced"
    exempt_account_ids: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "risk_threshold": self.risk_threshold,
            "ban_prob_threshold": self.ban_prob_threshold,
            "pause_risk_threshold": self.pause_risk_threshold,
            "pause_ban_threshold": self.pause_ban_threshold,
            "auto_pause": self.auto_pause,
            "notify_admin": self.notify_admin,
            "cool_duration_hours": self.cool_duration_hours,
            "cool_hours": self.cool_hours,
            "response_mode": self.response_mode,
            "preset": self.preset,
            "exempt_account_ids": list(self.exempt_account_ids),
        }


# ─── Decision dataclass ───────────────────────────────────────────────────────


@dataclass
class ShieldDecision:
    account_id: int
    risk_score: float
    ban_probability: float
    action: str  # "ok" | "warn" | "cool" | "pause" | "resume"
    note: str = ""
    level: str = ""  # что подсказал риск до учёта реакции (для честной записки)


# ─── DB helpers ───────────────────────────────────────────────────────────────


def _row_get(row, key, default=None):
    try:
        val = row.get(key) if hasattr(row, "get") else row[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def _detect_preset(cfg: ShieldConfig) -> str:
    for name, vals in PRESETS.items():
        if all(abs(float(getattr(cfg, k)) - float(v)) < 1e-6 for k, v in vals.items()):
            return name
    return "custom"


async def get_shield_config(pool: asyncpg.Pool, owner_id: int) -> ShieldConfig:
    """Read per-owner shield config; return defaults if not yet configured."""
    try:
        row = await pool.fetchrow(
            "SELECT * FROM shield_configs WHERE owner_id = $1",
            owner_id,
        )
        if row:
            auto_pause = bool(_row_get(row, "auto_pause", True))
            mode = str(_row_get(row, "response_mode", "") or "")
            if mode not in RESPONSE_MODES:
                # Строка до v323: флажок auto_pause — это и есть реакция.
                mode = "pause" if auto_pause else "cool"
            cfg = ShieldConfig(
                risk_threshold=float(_row_get(row, "risk_threshold", 0.7)),
                ban_prob_threshold=float(_row_get(row, "ban_prob_threshold", 0.5)),
                auto_pause=mode == "pause",
                notify_admin=bool(_row_get(row, "notify_admin", True)),
                cool_duration_hours=int(_row_get(row, "cool_duration_hours", 24)),
                pause_risk_threshold=float(_row_get(row, "pause_risk_threshold", 0.9)),
                pause_ban_threshold=float(_row_get(row, "pause_ban_threshold", 0.8)),
                cool_hours=int(_row_get(row, "cool_hours", 2)),
                response_mode=mode,
                exempt_account_ids=sorted({int(x) for x in (_row_get(row, "exempt_account_ids", []) or [])}),
            )
            cfg.preset = _detect_preset(cfg)
            return cfg
    except Exception as exc:
        log.debug("account_shield.get_shield_config owner=%d: %s", owner_id, exc)
    return ShieldConfig()


_SHIELD_FIELDS = {
    # поле: (минимум, максимум) для чисел; None — булево
    "risk_threshold": (0.0, 1.0),
    "ban_prob_threshold": (0.0, 1.0),
    "pause_risk_threshold": (0.0, 1.0),
    "pause_ban_threshold": (0.0, 1.0),
    "cool_duration_hours": (1, 168),
    "cool_hours": (1, 48),
    "auto_pause": None,
    "notify_admin": None,
    "response_mode": "mode",
    "exempt_account_ids": "ids",
}
_INT_FIELDS = ("cool_duration_hours", "cool_hours")


async def save_shield_config(pool: asyncpg.Pool, owner_id: int, **fields) -> ShieldConfig:
    """Сохранить настройки щита владельца и вернуть, что получилось.

    Писателя у `shield_configs` в мини-аппе не было вовсе: экран показывал
    пороги и переключатели, но поменять их было нечем — бот умел только две
    галочки. Пишем те поля, что пришли, остальные не трогаем.

    preset="careful|balanced|bold" раскрывается в свой набор порогов; поля,
    переданные рядом явно, важнее профиля. auto_pause и response_mode —
    две записи одной настройки, и пишутся они всегда вместе.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    preset = fields.pop("preset", None)
    if preset in PRESETS:
        for k, v in PRESETS[preset].items():
            fields.setdefault(k, v)
    if "response_mode" in fields:
        if fields["response_mode"] not in RESPONSE_MODES:
            fields.pop("response_mode")
        else:
            fields["auto_pause"] = fields["response_mode"] == "pause"
    elif "auto_pause" in fields:
        fields["response_mode"] = "pause" if fields["auto_pause"] else "cool"
    sets, params = [], []
    for name, bounds in _SHIELD_FIELDS.items():
        if name not in fields:
            continue
        val = fields[name]
        if bounds is None:
            val = bool(val)
        elif bounds == "mode":
            val = str(val)
        elif bounds == "ids":
            val = sorted({int(x) for x in (val or [])})
        elif name in _INT_FIELDS:
            val = max(bounds[0], min(bounds[1], int(val)))
        else:
            val = max(bounds[0], min(bounds[1], float(val)))
        params.append(val)
        sets.append(name)
    if not sets:
        return await get_shield_config(pool, owner_id)
    cols = ", ".join(sets)
    placeholders = ", ".join(f"${i + 2}" for i in range(len(sets)))
    updates = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(sets))
    await pool.execute(
        f"""INSERT INTO shield_configs (owner_id, {cols})
            VALUES ($1, {placeholders})
            ON CONFLICT (owner_id) DO UPDATE SET {updates}, updated_at=NOW()""",
        owner_id, *params,
    )
    return await get_shield_config(pool, owner_id)


async def _save_action(
    pool: asyncpg.Pool,
    owner_id: int,
    decision: ShieldDecision,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO shield_actions
               (owner_id, account_id, action, risk_score, ban_probability, note)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            owner_id,
            decision.account_id,
            decision.action,
            round(decision.risk_score, 4),
            round(decision.ban_probability, 4),
            decision.note or None,
        )
    except Exception as exc:
        log.debug(
            "account_shield._save_action acc=%d: %s", decision.account_id, exc
        )


# ─── Core evaluation ──────────────────────────────────────────────────────────


def _pct(x: float) -> str:
    return f"{round(float(x) * 100)}%"


def decide(risk: dict, cfg: ShieldConfig, account_id: int = 0) -> ShieldDecision:
    """Чистое решение щита по оценке риска и политике владельца.

    Записка `note` — по-русски и для владельца: она уходит в историю, в
    `status_reason` аккаунта и в Telegram. Раньше там стояло
    «risk=0.50 ban_prob=0.27 flood_rate_1h=0.00 — предупреждение».
    """
    risk_score = float(risk.get("risk_score") or 0.0)
    ban_prob = float(risk.get("ban_probability") or 0.0)

    if risk_score >= cfg.pause_risk_threshold or ban_prob >= cfg.pause_ban_threshold:
        level = "pause"
        why = (f"риск {_pct(risk_score)}, бан {_pct(ban_prob)} — выше порога паузы "
               f"({_pct(cfg.pause_risk_threshold)} / {_pct(cfg.pause_ban_threshold)})")
    elif risk_score >= cfg.risk_threshold or ban_prob >= cfg.ban_prob_threshold:
        level = "cool"
        why = (f"риск {_pct(risk_score)}, бан {_pct(ban_prob)} — выше порога охлаждения "
               f"({_pct(cfg.risk_threshold)} / {_pct(cfg.ban_prob_threshold)})")
    elif (risk_score >= cfg.risk_threshold * WARN_RATIO
          or ban_prob >= cfg.ban_prob_threshold * WARN_RATIO):
        level = "warn"
        why = f"риск {_pct(risk_score)}, бан {_pct(ban_prob)} — близко к порогу"
    else:
        level = "ok"
        why = ""

    # Реакция владельца ограничивает действие сверху — и записка говорит об
    # этом прямо, чтобы история не обещала паузу, которой не было.
    action = level
    if level == "pause" and cfg.response_mode != "pause":
        if cfg.response_mode == "cool":
            action = "cool"
            why += "; пауза выключена — только охлаждение"
        else:
            action = "warn"
            why += "; щит только предупреждает"
    elif level == "cool" and cfg.response_mode == "warn":
        action = "warn"
        why += "; щит только предупреждает"

    factors = risk.get("factors") or []
    if why and factors:
        top = factors[0]
        why += f". Главное: {top.get('label', '')}"
        if top.get("detail"):
            why += f" ({top['detail']})"

    if action == "pause":
        note = f"Пауза на {cfg.cool_duration_hours} ч: {why}"
    elif action == "cool":
        note = f"Охлаждение на {cfg.cool_hours} ч: {why}"
    elif action == "warn":
        note = f"Предупреждение: {why}"
    else:
        note = ""

    return ShieldDecision(
        account_id=account_id,
        risk_score=risk_score,
        ban_probability=ban_prob,
        action=action,
        note=note,
        level=level,
    )


async def evaluate_account(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int,
    cfg: ShieldConfig | None = None,
) -> ShieldDecision:
    """Evaluate one account and return the Shield decision.

    Uses physics_engine.get_account_risk() as the data source.
    Never raises.
    """
    risk = await physics_engine.get_account_risk(pool, account_id)
    if cfg is None:
        cfg = await get_shield_config(pool, owner_id)
    if account_id in set(cfg.exempt_account_ids or ()):
        return ShieldDecision(account_id=account_id,
                              risk_score=float(risk.get("risk_score") or 0.0),
                              ban_probability=float(risk.get("ban_probability") or 0.0),
                              action="ok", note="", level="exempt")
    return decide(risk, cfg, account_id)


# ─── Decision application ─────────────────────────────────────────────────────


async def apply_shield_decision(
    pool: asyncpg.Pool,
    decision: ShieldDecision,
    owner_id: int,
    *,
    cfg: ShieldConfig | None = None,
    record: bool = True,
) -> bool:
    """Apply the shield decision to tg_accounts and record the action.

    cool  — extend cooldown_until by 2h to lower effective priority
    pause — set is_active=FALSE for cool_duration_hours
    ok/warn — no structural change; just record the action

    ВОЗВРАЩАЕТ, ПРИМЕНИЛОСЬ ЛИ РЕШЕНИЕ, и это не удобство, а защита. Раньше
    функция возвращала None, сбой записи глотался в `log.debug`, а вызывающий
    всё равно считал аккаунт поставленным на паузу и говорил об этом
    владельцу. Для решений без структурной записи («warn») применять нечего,
    поэтому они честно возвращают True.
    """
    applied = True
    if cfg is None:
        cfg = await get_shield_config(pool, owner_id)

    if decision.action == "cool":
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET cooldown_until = GREATEST(
                           COALESCE(cooldown_until, NOW()),
                           NOW() + make_interval(hours => $3)
                       ),
                       status_reason = $2
                   WHERE id = $1""",
                decision.account_id,
                decision.note or "Защита аккаунтов: охлаждение",
                int(cfg.cool_hours),
            )
        except asyncio.CancelledError:
            _note_write_cancelled(
                "shield_cool", f"[acc={decision.account_id}]",
                "охлаждение опасного аккаунта не записано — он остаётся в "
                "полном темпе")
            raise
        except Exception as exc:
            applied = False
            _apply_failed("cool", decision.account_id, exc)

    elif decision.action == "pause":
        resume_at = datetime.now(timezone.utc) + timedelta(
            hours=cfg.cool_duration_hours
        )
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET is_active = FALSE,
                       -- Срок паузы ЩИТА: по нему цикл вернёт аккаунт сам.
                       -- cooldown_until паузой не продлеваем: иначе досрочный
                       -- возврат владельцем оставлял бы аккаунт в кулдауне до
                       -- конца паузы, а снимать кулдаун запрещено.
                       shield_paused_until = $2,
                       status_reason = $3
                   WHERE id = $1""",
                decision.account_id,
                resume_at,
                decision.note or f"Защита аккаунтов: пауза на {cfg.cool_duration_hours} ч",
            )
        except asyncio.CancelledError:
            _note_write_cancelled(
                "shield_pause", f"[acc={decision.account_id}]",
                "пауза опасного аккаунта не записана — он остаётся активным")
            raise
        except Exception as exc:
            applied = False
            _apply_failed("pause", decision.account_id, exc)

    if record:
        await _save_action(pool, owner_id, decision)
    return applied


# ─── Background worker ────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot) -> None:
    """Main loop: evaluate all active accounts every 30 minutes.

    For each owner:
      1. Fetch active accounts
      2. Evaluate each with physics_engine risk
      3. Apply decisions
      4. Notify admin (via bot.send_message) for "pause" actions if notify_admin=True
    """
    log.info("Account Shield started")
    while True:
        try:
            await _run_cycle(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Account Shield loop error: %s", exc)
        await asyncio.sleep(_LOOP_INTERVAL)


async def _recently_recorded(pool, account_id: int, action: str) -> bool:
    """Было ли это же решение по аккаунту недавно — тогда не пишем повтор."""
    try:
        return bool(await pool.fetchval(
            """SELECT 1 FROM shield_actions
               WHERE account_id=$1 AND action=$2
                 AND created_at > NOW() - make_interval(hours => $3)
               LIMIT 1""",
            account_id, action, _REPEAT_QUIET_HOURS,
        ))
    except asyncio.CancelledError:
        raise
    except Exception:
        return False



async def resume_expired(pool: asyncpg.Pool, owner_id: int) -> list[int]:
    """Вернуть в работу аккаунты, чья пауза щита истекла. Возвращает их id.

    Мёртвые за время паузы (бан, спам-блок, отозванная сессия) не
    возвращаются: метка с них просто снимается.
    """
    from services import account_status as _acc_status
    try:
        rows = await pool.fetch(
            """UPDATE tg_accounts
               SET is_active = TRUE,
                   shield_paused_until = NULL,
                   status_reason = CASE
                       WHEN status_reason LIKE 'Пауза на %'
                         OR status_reason LIKE 'Защита аккаунтов%'
                         OR status_reason LIKE 'Account Shield%'
                         OR status_reason LIKE 'risk=%'
                       THEN NULL ELSE status_reason END
               WHERE owner_id = $1
                 AND is_active = FALSE
                 AND shield_paused_until IS NOT NULL
                 AND shield_paused_until <= NOW()
                 AND session_str IS NOT NULL AND session_str <> ''
                 AND """ + _acc_status.sql_not_dead("acc_status") + """
               RETURNING id""",
            owner_id,
        )
        await pool.execute(
            """UPDATE tg_accounts SET shield_paused_until = NULL
               WHERE owner_id = $1 AND shield_paused_until IS NOT NULL
                 AND shield_paused_until <= NOW()""",
            owner_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("account_shield.resume_expired owner=%d: %s", owner_id, exc)
        return []
    ids = [int(r["id"]) for r in rows or []]
    for acc_id in ids:
        await _save_action(pool, owner_id, ShieldDecision(
            account_id=acc_id, risk_score=0.0, ban_probability=0.0,
            action="resume", note="Пауза щита закончилась — аккаунт снова в работе"))
    return ids


async def resume_now(pool: asyncpg.Pool, owner_id: int, account_id: int) -> bool:
    """Владелец сам возвращает аккаунт из паузы щита. Только свой аккаунт."""
    res = await pool.execute(
        """UPDATE tg_accounts
           SET is_active = TRUE,
               status_reason = CASE
                   WHEN status_reason LIKE 'Пауза на %'
                     OR status_reason LIKE 'Охлаждение на %'
                     OR status_reason LIKE 'Защита аккаунтов%'
                     OR status_reason LIKE 'Account Shield%'
                     OR status_reason LIKE 'risk=%'
                   THEN NULL ELSE status_reason END,
               -- cooldown_until НЕ трогаем: его могли поставить флуд и
               -- спам-сигналы, а пауза аккаунта никогда не укорачивается
               -- (tests/test_account_pause_is_never_shortened.py). Пауза щита
               -- держится на is_active + shield_paused_until, их и снимаем.
               shield_paused_until = NULL
           WHERE id = $1 AND owner_id = $2
             AND is_active = FALSE
             AND shield_paused_until IS NOT NULL""",
        account_id, owner_id,
    )
    try:
        ok = int(str(res).split()[-1]) > 0
    except (ValueError, IndexError):
        ok = False
    if ok:
        await _save_action(pool, owner_id, ShieldDecision(
            account_id=account_id, risk_score=0.0, ban_probability=0.0,
            action="resume", note="Владелец вернул аккаунт в работу"))
    return ok


async def run_owner(pool: asyncpg.Pool, owner_id: int, bot=None) -> dict:
    """Один проход щита по флоту владельца. Возвращает счётчики итога.

    Его зовёт и фоновый цикл, и кнопка «Проверить сейчас».
    """
    out = {"checked": 0, "paused": 0, "cooled": 0, "warned": 0,
           "resumed": 0, "failed": 0, "exempt": 0}
    out["resumed"] = len(await resume_expired(pool, owner_id))
    try:
        await physics_engine.compute_owner(pool, owner_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Оценка не обновилась — решаем по последней сохранённой.
        log.warning("account_shield: пересчёт риска owner=%d: %s", owner_id, exc)
    from services import account_status as _acc_status
    accounts = await pool.fetch(
        """SELECT id FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE
             AND """ + _acc_status.sql_not_dead("acc_status"),
        owner_id,
    )
    cfg = await get_shield_config(pool, owner_id)
    for acc_row in accounts:
        account_id: int = acc_row["id"]
        out["checked"] += 1
        try:
            decision = await evaluate_account(pool, account_id, owner_id, cfg)
            if decision.level == "exempt":
                out["exempt"] += 1
                continue
            if decision.action not in ("cool", "pause", "warn"):
                # don't record "ok" to avoid bloating shield_actions table
                continue
            # Повтор того же решения по аккаунту в истории не пишем (и в
            # Telegram не шлём); охлаждение при этом продлевается как раньше.
            repeat = decision.action != "pause" and await _recently_recorded(
                pool, account_id, decision.action)
            # По ФАКТУ, а не по решению: см. apply_shield_decision.
            applied = await apply_shield_decision(
                pool, decision, owner_id, cfg=cfg, record=not repeat)
            if decision.action == "pause":
                if applied:
                    out["paused"] += 1
                else:
                    out["failed"] += 1
                if cfg.notify_admin and bot is not None:
                    await _notify_pause(
                        bot, pool, owner_id, decision, cfg,
                        applied=applied,
                    )
            elif decision.action == "cool":
                if applied:
                    out["cooled"] += 1
                else:
                    out["failed"] += 1
            else:
                out["warned"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(
                "account_shield: eval error acc=%d: %s", account_id, exc
            )
        await asyncio.sleep(0.05)
    return out


async def _run_cycle(pool: asyncpg.Pool, bot) -> None:
    # Владельцы с активными аккаунтами И с аккаунтами на паузе щита: у
    # владельца, чей флот целиком на паузе, иначе её никто бы не снял.
    try:
        owners = await pool.fetch(
            """SELECT DISTINCT owner_id FROM tg_accounts
               WHERE is_active=TRUE OR shield_paused_until IS NOT NULL"""
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("account_shield._run_cycle owners: %s", exc)
        return

    paused_total = cooled_total = failed_total = resumed_total = 0
    for owner_row in owners:
        owner_id: int = owner_row["owner_id"]
        try:
            res = await run_owner(pool, owner_id, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("account_shield._run_cycle owner=%d: %s", owner_id, exc)
            continue
        paused_total += res["paused"]
        cooled_total += res["cooled"]
        failed_total += res["failed"]
        resumed_total += res["resumed"]

    if paused_total or cooled_total or resumed_total:
        log.info(
            "Account Shield cycle: paused=%d cooled=%d resumed=%d",
            paused_total, cooled_total, resumed_total,
        )
    if failed_total:
        # Отдельной строкой и громко: это аккаунты, которые Щит признал
        # опасными и НЕ смог остановить.
        log.error(
            "Account Shield cycle: %d решений не применились — эти аккаунты "
            "остаются активными", failed_total,
        )


async def _notify_pause(
    bot,
    pool: asyncpg.Pool,
    owner_id: int,
    decision: ShieldDecision,
    cfg: ShieldConfig,
    applied: bool = True,
) -> None:
    """Сообщить владельцу об автопаузе — или о том, что её НЕ удалось поставить.

    Прежний текст («Аккаунт поставлен на паузу») уходил и тогда, когда запись
    не прошла. Владелец, прочитав его, перестаёт следить за аккаунтом — ровно
    за тем, который сейчас сгорит. Поэтому текст теперь зависит от факта, а
    при неудаче прямо просит остановить аккаунт вручную.
    """
    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name, username FROM tg_accounts WHERE id=$1",
            decision.account_id,
        )
        if acc:
            name = (
                f"@{acc['username']}"
                if acc.get("username")
                else (acc.get("first_name") or acc.get("phone") or f"id{decision.account_id}")
            )
        else:
            name = f"id{decision.account_id}"

        if applied:
            text = (
                "🛡 <b>Защита аккаунтов — автопауза</b>\n\n"
                f"Аккаунт <b>{name}</b> поставлен на паузу.\n\n"
                f"📊 Риск: <b>{decision.risk_score:.0%}</b>\n"
                f"⚠️ Вероятность бана: <b>{decision.ban_probability:.0%}</b>\n"
                f"⏸ Пауза на: <b>{cfg.cool_duration_hours} ч</b> — потом щит "
                "сам вернёт аккаунт в работу\n\n"
                f"<i>{decision.note}</i>"
            )
        else:
            text = (
                "🛡 <b>Защита аккаунтов: опасный аккаунт НЕ остановлен</b>\n\n"
                f"Аккаунт <b>{name}</b> признан опасным, но запись паузы не "
                "прошла — он остаётся активным и может уйти в следующую "
                "операцию.\n\n"
                f"📊 Риск: <b>{decision.risk_score:.0%}</b>\n"
                f"⚠️ Вероятность бана: <b>{decision.ban_probability:.0%}</b>\n\n"
                "Отключите аккаунт вручную в списке аккаунтов.\n\n"
                f"<i>{decision.note}</i>"
            )
        # Общий слой: тумблер «Ограничения аккаунтов», уровень «нужно действие»
        # (идёт и в тихие часы). Ключ — аккаунт: паузы разных аккаунтов не
        # делят один слот анти-спама.
        from database import db as _db
        await _db.notify_if_enabled(pool, bot, owner_id, "restriction", text,
                                    dedup_key=f"shield:{decision.account_id}:{bool(applied)}")
    except Exception as exc:
        log.debug("account_shield._notify_pause owner=%d: %s", owner_id, exc)
