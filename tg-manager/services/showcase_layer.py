"""Витрина — буфер между дочерней группой и боевым каналом.

Зачем нужен ещё один слой.

«Мать-Дочка» закрывает ОДИН вектор: бан за сам инвайт достаётся расходной
дочерней группе, а не боевому каналу. Но она открывает второй, о котором до сих
пор нигде не говорилось: приглашённые видят в дочерней закреплённый редирект и
идут в мать ПО ОДНОЙ ССЫЛКЕ. Двести человек за два часа одним потоком — тот
самый сигнал, по которому канал и закрывают. От бана за инвайты защитили, от
всплеска вступлений — нет.

Почему промежуточный слой сам по себе не помогает. Если витрина просто
закрепляет ссылку на мать, те же люди перейдут по той же ссылке — бросок
сдвинется на шаг, но не станет меньше. Лишний узел даёт косвенность, а не темп.

Что здесь на самом деле работает. В канале нельзя показать пост «части
аудитории»: он виден всем сразу. Поэтому волнами раздаётся не показ, а
ПРОПУСКНАЯ СПОСОБНОСТЬ — ссылка на мать выпускается с лимитом вступлений
(account_manager.create_channel_invite_link умеет usage_limit и срок жизни).
Исчерпалась — следующая волна выпускает новую. Сколько людей войдёт в час,
решает система, а не скорость кликов.

Числа ниже — инженерное суждение, а не измеренная константа: Telegram пределы
скорости вступлений не публикует. Поэтому они консервативны, привязаны к РАЗМЕРУ
канала (для маленького канала +50 человек за час заметнее, чем для большого) и
вынесены в env — их можно двигать, не трогая код.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Доля от текущего размера канала, которую считаем безопасным приростом за волну.
_WAVE_SHARE = 0.05
# Границы волны: маленькому каналу нужен пол (иначе рост встанет совсем),
# большому — потолок (иначе «5% от 100 000» снова даст всплеск).
_WAVE_MIN = 10
_WAVE_MAX = 50
# Пауза между волнами.
_WAVE_INTERVAL_SEC = 30 * 60


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def wave_size(mother_members: int) -> int:
    """Сколько человек пускаем в боевой канал за одну волну.

    Привязано к размеру: +50 к каналу на 200 участников — это +25% за полчаса,
    заметный скачок; для канала на 50 000 те же 50 человек не видны вовсе.
    """
    lo = max(1, _env_int("SHOWCASE_WAVE_MIN", _WAVE_MIN))
    hi = max(lo, _env_int("SHOWCASE_WAVE_MAX", _WAVE_MAX))
    try:
        members = max(0, int(mother_members or 0))
    except (TypeError, ValueError):
        members = 0
    return max(lo, min(hi, int(members * _WAVE_SHARE)))


def wave_interval_sec() -> int:
    """Пауза между волнами. Нижняя граница не даёт выродить темп в «сразу всё»."""
    return max(60, _env_int("SHOWCASE_WAVE_INTERVAL_SEC", _WAVE_INTERVAL_SEC))


def next_wave_due(last_wave_ts: float | None, now_ts: float) -> bool:
    """Пора ли выпускать следующую волну.

    Первая волна идёт сразу: буфер без единой открытой двери — это не темп,
    а остановка.
    """
    if not last_wave_ts:
        return True
    try:
        return (float(now_ts) - float(last_wave_ts)) >= wave_interval_sec()
    except (TypeError, ValueError):
        return True


def release_plan(audience_left: int, mother_members: int) -> dict:
    """План ближайшей волны: сколько пускаем и на сколько это растянется.

    Возвращает {"size": int, "waves_left": int, "eta_sec": int} — числа нужны
    интерфейсу, чтобы владелец видел цену темпа ДО запуска, а не узнавал о ней
    по ходу.
    """
    try:
        left = max(0, int(audience_left or 0))
    except (TypeError, ValueError):
        left = 0
    size = wave_size(mother_members)
    waves = (left + size - 1) // size if left else 0
    # Первая волна уходит сразу, поэтому ждать надо на одну паузу меньше.
    eta = max(0, waves - 1) * wave_interval_sec()
    return {"size": size, "waves_left": waves, "eta_sec": eta}


def humanize_eta(eta_sec: int) -> str:
    """«за 3 ч 30 мин» — чтобы цена темпа читалась без арифметики."""
    try:
        sec = max(0, int(eta_sec or 0))
    except (TypeError, ValueError):
        sec = 0
    if sec < 60:
        return "меньше минуты"
    hours, minutes = divmod(sec // 60, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


# ── работа с базой и Telegram ────────────────────────────────────────────────
#
# Ниже — та часть, что ходит наружу. Она намеренно отделена от чистой логики
# выше: темп можно проверить без сети и базы, и именно он определяет
# безопасность, а не количество звеньев в цепочке.

async def get_or_create(pool, owner_id: int, mother_ref: str, creator_acc: dict) -> dict:
    """Активная витрина пары (владелец, мать) либо новая.

    Возвращает {"ok": True, "showcase_ref": str, "id": int} либо
    {"ok": False, "error": str}. Форма ответа повторяет daughter_groups —
    вызывающему не надо помнить два разных контракта.
    """
    row = await pool.fetchrow(
        "SELECT id, showcase_ref FROM showcase_layers "
        "WHERE owner_id=$1 AND mother_ref=$2 AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        owner_id, mother_ref,
    )
    if row:
        return {"ok": True, "showcase_ref": row["showcase_ref"], "id": int(row["id"])}

    from services import account_manager

    res = await account_manager.create_channel(
        creator_acc["session_str"], "Chat", "", True, dict(creator_acc))
    if res.get("error") or not res.get("channel_id"):
        err = res.get("error") or "create_channel вернул пустой результат"
        log.warning("showcase_layer: создание витрины не удалось: %s", err)
        return {"ok": False, "error": err}

    channel_id = int(res["channel_id"])
    access_hash = int(res.get("access_hash") or 0)

    link_res = await account_manager.create_channel_invite_link(
        creator_acc["session_str"], channel_id, dict(creator_acc), access_hash)
    if not link_res.get("ok") or not link_res.get("link"):
        err = link_res.get("error") or "ссылка не выпущена"
        log.warning("showcase_layer: ссылка на витрину %s не выпущена: %s", channel_id, err)
        return {"ok": False, "error": err}

    row = await pool.fetchrow(
        "INSERT INTO showcase_layers(owner_id, mother_ref, channel_id, access_hash, "
        "showcase_ref, creator_account_id, status) "
        "VALUES($1,$2,$3,$4,$5,$6,'active') RETURNING id",
        owner_id, mother_ref, channel_id, access_hash, link_res["link"],
        int(creator_acc["id"]),
    )
    log.info("showcase_layer: витрина создана для матери %s", mother_ref[:80])
    return {"ok": True, "showcase_ref": link_res["link"], "id": int(row["id"])}


async def release_wave(pool, showcase_id: int, mother_members: int,
                       creator_acc: dict, mother_channel_id: int,
                       mother_access_hash: int = 0) -> dict:
    """Выпустить волну: ссылка в мать С ЛИМИТОМ вступлений, пост в витрину.

    Лимит — и есть темп. Пока ссылка не исчерпана, новую не выпускаем: иначе
    несколько действующих ссылок сложатся в тот же всплеск, от которого уходим.
    """
    from services import account_manager

    row = await pool.fetchrow(
        "SELECT channel_id, access_hash, last_wave_at, waves_released "
        "FROM showcase_layers WHERE id=$1 AND status='active'", showcase_id)
    if not row:
        return {"ok": False, "error": "витрина не найдена или сожжена"}

    size = wave_size(mother_members)
    link_res = await account_manager.create_channel_invite_link(
        creator_acc["session_str"], mother_channel_id, dict(creator_acc),
        mother_access_hash, title="showcase-wave", usage_limit=size)
    if not link_res.get("ok") or not link_res.get("link"):
        err = link_res.get("error") or "ссылка волны не выпущена"
        log.warning("showcase_layer: волна для витрины %s не выпущена: %s", showcase_id, err)
        return {"ok": False, "error": err}

    posted = await _post_wave(
        creator_acc["session_str"], int(row["channel_id"]), int(row["access_hash"] or 0),
        link_res["link"], size, dict(creator_acc))

    await pool.execute(
        "UPDATE showcase_layers SET last_wave_at=now(), waves_released=waves_released+1, "
        "active_link=$2, active_link_limit=$3 WHERE id=$1",
        showcase_id, link_res["link"], size,
    )
    return {"ok": True, "size": size, "link": link_res["link"], "posted": posted}


async def _post_wave(session_string: str, channel_id: int, access_hash: int,
                     link: str, size: int, _acc: dict) -> bool:
    """Положить ссылку волны в витрину. Провал не критичен: ссылка уже выпущена
    и учтена, следующая волна перекроет — ронять прогон из-за поста незачем."""
    import asyncio

    from services.account_manager import _make_client, _CONNECT_TIMEOUT

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        from telethon.tl.types import InputChannel

        channel = InputChannel(channel_id=channel_id, access_hash=access_hash)
        await client.send_message(
            channel,
            f"🔓 Открыт вход в основной канал — {size} мест: {link}\n"
            "Места кончатся — следующая порция откроется позже.",
        )
        return True
    except Exception as e:
        log.warning("showcase_layer: пост волны в витрину %s не ушёл: %s", channel_id, e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def mark_burned(pool, showcase_id: int, reason: str) -> None:
    await pool.execute(
        "UPDATE showcase_layers SET status='burned', burned_at=now(), burn_reason=$2 "
        "WHERE id=$1 AND status='active'",
        showcase_id, (reason or "")[:200],
    )
