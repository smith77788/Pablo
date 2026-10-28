"""Связки модулей: итог одной операции становится входом следующей.

Разрыв, который это закрывает. Модули массовых операций жили островами:
парсинг собирал базу, и чтобы пригласить ИМЕННО её, человек шёл в инвайтер и
выбирал базу из списка заново; авторегистрация создавала аккаунты, и чтобы их
прогреть, нужно было найти каждый в списке; созданные каналы надо было
вручную собирать в папку и вручную же отдавать виртуальному администратору.
Продукт знал, что именно получилось (база №, id аккаунтов, id каналов), — и
выбрасывал это знание в момент завершения операции.

Контракт передачи. Исполнитель-производитель кладёт в свой результат ключ
``handoff`` — что он произвёл, в машинном виде:

* ``parse_run_id`` — база парсера (``parser_runs.id``);
* ``account_ids``  — созданные аккаунты (``tg_accounts.id``);
* ``channel_ids``  — созданные каналы/группы (Telegram ``channel_id``),
  ``chan_kind`` — ``channel`` или ``group``.

Отсюда строятся шаги двух видов:

* ``launch`` — следующая операция запускается одним нажатием, а её цели
  берутся из ``handoff`` на сервере. Клиент присылает только номер исходной
  операции и id шага; параметры он не присылает и подменить не может.
  Владение каждой цели перепроверяется в запускателе.
* ``open`` — следующий модуль требует ввода человека (куда приглашать, какой
  текст разослать), поэтому открывается его экран, уже заполненный итогом.
  Экран задаётся словарём разделов мини-аппа ``kind:param`` — тем же, что
  понимают уведомления бота (``#kind:param`` в web_app-ссылке), поэтому обе
  двери ведут в одно и то же место одной записью.

Обе двери (бот — ``bot/handlers/op_chain.py``, мини-апп —
``op_quick_actions`` и ``/api/miniapp/op_chain/launch``) берут шаги ТОЛЬКО
отсюда и запускают их ТОЛЬКО через ``launch``.

``steps_for`` — чистая функция, проверяется без базы и сети.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

HANDOFF = "handoff"

# Сколько целей переносим в следующий шаг. Авторегистрация ограничена 50
# аккаунтами за прогон, создание каналов — тем же порядком; потолок нужен
# только чтобы испорченный результат не превратился в тысячу операций.
_MAX_IDS = 200

_PRODUCTIVE = {"done", "partial"}


def _ids(v) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for x in (v or []) if isinstance(v, (list, tuple)) else []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
        if len(out) >= _MAX_IDS:
            break
    return out


def as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)) and v:
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def handoff_of(result) -> dict:
    """Что произвела операция (пустой словарь, если ничего не передаёт)."""
    return as_dict(as_dict(result).get(HANDOFF))


def _open(sid: str, label: str, reason: str, screen: str, bot: dict | None = None) -> dict:
    s = {"id": sid, "kind": "open", "label": label, "reason": reason, "screen": screen}
    if bot:
        s["bot"] = bot
    return s


def _launch(sid: str, label: str, reason: str, launcher: str, data: dict,
            confirm: str) -> dict:
    return {"id": sid, "kind": "launch", "label": label, "reason": reason,
            "launcher": launcher, "data": data, "confirm": confirm}


def steps_for(op_type: str, status: str, params: dict | None = None,
              result: dict | None = None) -> list[dict]:
    """Следующие шаги по итогу операции. Порядок = приоритет показа.

    Шаги появляются только у операции, которая что-то произвела: провал или
    отмена без результата не дают входа следующему модулю.
    """
    st = (status or "").strip().lower()
    if st not in _PRODUCTIVE:
        return []
    res = as_dict(result)
    ho = handoff_of(res)
    out: list[dict] = []

    if op_type == "parse_audience":
        try:
            run_id = int(ho.get("parse_run_id") or 0)
        except (TypeError, ValueError):
            run_id = 0
        try:
            saved = int(res.get("total_saved") or 0)
        except (TypeError, ValueError):
            saved = 0
        if run_id > 0 and saved > 0:
            out.append(_open(
                "inv_run", f"➕ Пригласить эту базу ({saved})",
                "база уже выбрана — останется указать группу",
                f"invite_run:{run_id}",
                bot={"cb": "inviter_from_run", "arg": run_id}))
            out.append(_open(
                "cmp_run", f"📨 Разослать этой базе ({saved})",
                "аудитория уже выбрана — останется написать текст",
                f"campaign_run:{run_id}"))

    elif op_type == "auto_register":
        acc = _ids(ho.get("account_ids"))
        if acc:
            n = len(acc)
            out.append(_launch(
                "warm_new", f"🔥 Мягко прогреть новые ({n})",
                "свежий аккаунт без прогрева первым уходит в бан",
                "warmup_accounts", {"account_ids": acc},
                f"Запустить мягкий прогрев для {n} новых аккаунтов?"))
            out.append(_launch(
                "check_new", f"🩺 Проверить новые ({n})",
                "сразу видно, кого Telegram ограничил после регистрации",
                "check_accounts", {"account_ids": acc},
                f"Проверить здоровье {n} новых аккаунтов (в т.ч. через @SpamBot)?"))

    elif op_type == "bulk_create_channels":
        chans = _ids(ho.get("channel_ids"))
        is_group = (ho.get("chan_kind") == "group")
        n = len(chans)
        if n >= 2:
            out.append(_launch(
                "folder", f"📁 Собрать в общую папку ({n})",
                "все новые ресурсы раздаются одной ссылкой",
                "folder", {"channel_ids": chans,
                           "title": "Новые группы" if is_group else "Новые каналы"},
                f"Собрать {n} новых {'групп' if is_group else 'каналов'} в общую папку?"))
        if n == 1:
            out.append(_open(
                "inv_chan", "➕ Наполнить участниками",
                "группа для инвайта уже подставлена",
                f"invite_channel:{chans[0]}"))
        if chans and not is_group:
            out.append(_open(
                "va", "🧠 Поручить администратору",
                "виртуальный администратор будет вести новый канал сам",
                f"va_channel:{chans[0]}" if n == 1 else "va"))
        if n > 1:
            out.append(_open("chans", "📡 Открыть каналы", "новые каналы в списке",
                             "channels"))

    elif op_type == "scan_owned_bots":
        try:
            new = int(res.get("new") or 0)
        except (TypeError, ValueError):
            new = 0
        if new > 0:
            out.append(_launch(
                "connect", f"🔌 Подключить найденных ({new})",
                "найденные боты начнут работать в Infragram",
                "connect_bots", {"limit": min(50, new)},
                "Подключить найденных ботов? Infragram спросит их токены у "
                "@BotFather от лица аккаунтов-владельцев."))

    return out


def public_step(step: dict) -> dict:
    """Шаг без серверных данных — то, что уходит клиенту."""
    return {k: v for k, v in step.items() if k != "data"}


# ── Запуск шага ──────────────────────────────────────────────────────────────

async def _owned_accounts(pool, owner_id: int, ids: list[int], active_only: bool) -> list[int]:
    if not ids:
        return []
    q = "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])"
    if active_only:
        q += " AND is_active=TRUE"
    rows = await pool.fetch(q + " ORDER BY id", owner_id, ids)
    return [int(r["id"]) for r in rows]


async def _launch_warmup(pool, owner_id: int, data: dict, src_op: int) -> dict:
    from services import operation_bus as _obus

    acc = await _owned_accounts(pool, owner_id, _ids(data.get("account_ids")), True)
    if not acc:
        return {"ok": False, "reason": "Новых активных аккаунтов не осталось"}
    op_ids = []
    for a in acc:
        op_ids.append(await _obus.submit(
            pool, owner_id, "account_warmup",
            {"account_id": a, "plan_type": "gentle", "chain_from": src_op},
            total_items=1, label=f"Мягкий прогрев нового аккаунта #{a}"))
    return {"ok": True, "op_ids": op_ids,
            "message": f"🔥 Прогрев запущен для {len(acc)} аккаунтов"}


async def _launch_check(pool, owner_id: int, data: dict, src_op: int) -> dict:
    from services import operation_bus as _obus

    acc = await _owned_accounts(pool, owner_id, _ids(data.get("account_ids")), False)
    if not acc:
        return {"ok": False, "reason": "Этих аккаунтов больше нет"}
    op_id = await _obus.submit(
        pool, owner_id, "check_accounts_health",
        {"account_ids": acc, "check_spambot": True, "chain_from": src_op},
        total_items=len(acc), label=f"Проверка {len(acc)} новых аккаунтов")
    return {"ok": True, "op_ids": [op_id],
            "message": f"🩺 Проверка запущена для {len(acc)} аккаунтов"}


async def _launch_folder(pool, owner_id: int, data: dict, src_op: int) -> dict:
    from services import chatlist_folders as cf

    r = await cf.submit_folder(pool, owner_id, _ids(data.get("channel_ids")),
                               data.get("title") or "Новые каналы")
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("reason") or "Папку собрать не удалось"}
    return {"ok": True, "op_ids": [r["op_id"]],
            "message": f"📁 Собираю папку из {r['chat_count']} чатов"}


async def _launch_connect_bots(pool, owner_id: int, data: dict, src_op: int) -> dict:
    from services import operation_bus as _obus

    pending = await pool.fetchval(
        "SELECT COUNT(*) FROM discovered_bots "
        "WHERE owner_id=$1 AND linked_bot_id IS NULL AND acc_id IS NOT NULL", owner_id)
    if not pending:
        return {"ok": False, "reason": "Неподключённых найденных ботов уже нет"}
    try:
        limit = max(1, min(50, int(data.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    op_id = await _obus.submit(
        pool, owner_id, "connect_discovered_bots",
        {"usernames": [], "limit": limit},
        total_items=min(int(pending), limit), label="Подключение найденных ботов")
    return {"ok": True, "op_ids": [op_id], "message": "🔌 Подключение запущено"}


_LAUNCHERS = {
    "warmup_accounts": _launch_warmup,
    "check_accounts": _launch_check,
    "folder": _launch_folder,
    "connect_bots": _launch_connect_bots,
}


async def steps_for_op(pool, owner_id: int, op_id: int) -> tuple[str | None, list[dict]]:
    """Шаги по операции из базы (owner-scoped). (op_type, шаги)."""
    row = await pool.fetchrow(
        "SELECT op_type, status, params, result FROM operation_queue "
        "WHERE id=$1 AND owner_id=$2", int(op_id), int(owner_id))
    if not row:
        return None, []
    return row["op_type"], steps_for(row["op_type"], row["status"],
                                     as_dict(row["params"]), as_dict(row["result"]))


async def launch(pool, owner_id: int, op_id: int, step_id: str) -> dict:
    """Запустить шаг-связку по итогу операции. Единая точка для обеих дверей.

    Шаг пересчитывается по сохранённому результату операции этого владельца —
    цели берутся оттуда, а не от клиента. Никогда не бросает:
    {"ok", "op_ids"?, "message"?, "reason"?}.
    """
    try:
        _t, steps = await steps_for_op(pool, owner_id, op_id)
    except Exception:
        log.exception("op_chain: шаги для op=%s не прочитались", op_id)
        return {"ok": False, "reason": "Не удалось прочитать операцию"}
    if _t is None:
        return {"ok": False, "reason": "Операция не найдена"}
    step = next((s for s in steps if s["id"] == step_id and s["kind"] == "launch"), None)
    if not step:
        return {"ok": False, "reason": "Этот шаг для операции недоступен"}
    fn = _LAUNCHERS.get(step["launcher"])
    if fn is None:  # pragma: no cover - защищено тестом реестра
        return {"ok": False, "reason": "Шаг не поддержан"}
    try:
        return await fn(pool, int(owner_id), step["data"], int(op_id))
    except PermissionError as exc:
        # Отказ по тарифу — не сбой, причину показываем как есть.
        return {"ok": False, "reason": str(exc) or "Требуется подписка"}
    except Exception:
        log.exception("op_chain: запуск шага %s по op=%s", step_id, op_id)
        return {"ok": False, "reason": "Не удалось запустить шаг"}
