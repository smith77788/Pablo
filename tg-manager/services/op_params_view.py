"""Параметры операции — человеческим языком, для карточки операции.

ЗАЧЕМ. Карточка операции показывала статус, прогресс и итог, но не то, ЧТО
именно запускали: какой текст ушёл, в какие каналы, с каких аккаунтов, с какой
паузой. Ответ на главный вопрос после неудачи — «а с какими настройками я это
запускал?» — лежал в params операции и до экрана не доходил вообще.

КАК. Известные ключи params переводятся в строки «подпись — значение».
Списки называются числом и первыми значениями, а не вываливаются целиком.
Незнакомые и служебные ключи (отметки прогона, id исходной операции) не
показываются: строка, которую человек не может понять, хуже отсутствующей.
Секреты (токены, сессии, прокси, пароли) не показываются НИКОГДА — даже если
попали в params по ошибке.

Модуль ЧИСТЫЙ: без БД и сети, тестируется напрямую.
"""
from __future__ import annotations

from typing import Any

# Ключи, значения которых нельзя показывать ни при каких условиях.
_SECRET_PARTS = ("token", "session", "proxy", "password", "secret", "api_key", "hash")

# Служебные ключи: отметки прогона и связи между операциями — человеку не нужны.
_INTERNAL = frozenset({
    "owner_id", "source_op_id", "launched_bot_ids", "launched_bc_ids",
    "journal_op_ids", "approval_token", "channel_acc_pairs", "run_now",
    "strike_chain", "retry_of", "scheduled_for",
})

_TARGET_RU = {"all": "каналы и группы", "channels": "только каналы", "groups": "только группы"}
_DELAY_MODE_RU = {"fast": "быстро", "normal": "обычно", "slow": "медленно",
                  "smart": "умная (по состоянию аккаунтов)", "safe": "бережно"}
_SEGMENT_RU = {"all": "все подписчики", "all_each": "все подписчики каждого бота",
               "unique": "уникальные люди", "selected_bots": "выбранные боты",
               "cluster": "кластер ботов", "active_7d": "активные за 7 дней",
               "active_30d": "активные за 30 дней", "inactive_14d": "спящие (14+ дней)",
               "cold_all": "холодные", "lost_all": "ушедшие"}

# (ключ, подпись, вид значения). Порядок — порядок строк на экране.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("text", "Текст", "text"),
    ("text_to_post", "Текст поста", "text"),
    ("message_text", "Текст", "text"),
    ("name", "Имя", "text"),
    ("title", "Название", "text"),
    ("description", "Описание", "text"),
    ("short_description", "Краткое описание", "text"),
    ("about", "Описание", "text"),
    ("greeting", "Приветствие", "text"),
    ("welcome", "Приветствие", "text"),
    ("value", "Новое значение", "text"),
    ("field", "Поле", "text"),
    ("source", "Источник", "text"),
    ("source_ref", "Источник", "text"),
    ("channel", "Канал", "text"),
    ("channel_ref", "Канал", "text"),
    ("niche", "Ниша", "text"),
    ("target", "Куда", "target"),
    ("segment", "Аудитория", "segment"),
    ("channel_ids", "Каналы", "list"),
    ("target_refs", "Цели", "list"),
    ("targets", "Цели", "list"),
    ("links", "Чаты", "list"),
    ("channels", "Каналы", "list"),
    ("usernames", "Получатели", "list"),
    ("user_refs", "Люди", "list"),
    ("user_ids", "Люди", "list"),
    ("phones", "Номера", "list"),
    ("bot_ids", "Боты", "list"),
    ("selected_bot_ids", "Боты", "list"),
    ("msg_ids", "Сообщения", "list"),
    ("account_ids", "Аккаунты", "list"),
    ("acc_id", "Аккаунт", "account"),
    ("account_id", "Аккаунт", "account"),
    ("count", "Количество", "num"),
    ("acc_count", "Аккаунтов", "num"),
    ("bot_count", "Ботов на аккаунт", "num"),
    ("limit", "Лимит", "num"),
    ("msg_count", "Сообщений", "num"),
    ("delay", "Пауза между действиями", "seconds"),
    ("delay_seconds", "Пауза между действиями", "seconds"),
    ("delay_mode", "Темп", "delay_mode"),
    ("repeat_interval_min", "Повтор", "repeat"),
    ("repeat_count", "Повторов", "num"),
    ("silent", "Без звука", "bool"),
    ("premium_only", "Только Premium", "bool"),
    ("skip_invited", "Пропускать уже приглашённых", "bool"),
    ("use_daughter_groups", "Через дочерние группы", "bool"),
    ("is_group", "Создавать группы", "bool"),
    ("all", "Все", "bool"),
    ("buttons", "Кнопки", "buttons"),
    ("pillar", "Рубрика", "text"),
)

_LIST_PREVIEW = 3
_TEXT_MAX = 300


def _secret(key: str) -> bool:
    k = key.lower()
    return any(p in k for p in _SECRET_PARTS)


def _short(v: Any, n: int = _TEXT_MAX) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _fmt(kind: str, v: Any) -> str | None:
    if v is None or v == "" or v == [] or v == {}:
        return None
    if kind == "text":
        return _short(v)
    if kind == "list":
        items = v if isinstance(v, (list, tuple)) else [v]
        items = [x for x in items if x not in (None, "")]
        if not items:
            return None
        head = ", ".join(_short(x, 40) for x in items[:_LIST_PREVIEW])
        more = len(items) - _LIST_PREVIEW
        return f"{len(items)}: {head}" + (f" и ещё {more}" if more > 0 else "")
    if kind == "account":
        return f"#{v}"
    if kind == "num":
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return "все" if n == 0 else str(n)
    if kind == "seconds":
        try:
            s = int(v)
        except (TypeError, ValueError):
            return None
        if s < 0:
            return "без паузы"
        return f"{s} {_plural(s, 'секунда', 'секунды', 'секунд')}"
    if kind == "repeat":
        try:
            m = int(v)
        except (TypeError, ValueError):
            return None
        if m <= 0:
            return None
        if m % 1440 == 0:
            d = m // 1440
            return f"каждые {d} {_plural(d, 'день', 'дня', 'дней')}" if d > 1 else "каждый день"
        if m % 60 == 0:
            h = m // 60
            return f"каждые {h} {_plural(h, 'час', 'часа', 'часов')}" if h > 1 else "каждый час"
        return f"каждые {m} мин"
    if kind == "bool":
        return "да" if v is True or str(v).lower() in ("1", "true", "yes") else None
    if kind == "target":
        return _TARGET_RU.get(str(v), None)
    if kind == "segment":
        return _SEGMENT_RU.get(str(v), None)
    if kind == "delay_mode":
        return _DELAY_MODE_RU.get(str(v), None)
    if kind == "buttons":
        if not isinstance(v, list):
            return None
        names = [_short((b or {}).get("text") or "", 30) for b in v if isinstance(b, dict)]
        names = [x for x in names if x]
        return f"{len(names)}: " + ", ".join(names[:_LIST_PREVIEW]) if names else None
    return None


def describe(op_type: str | None, params: Any) -> list[dict]:
    """[{label, value}] для экрана. Пустой список — показывать нечего."""
    if not isinstance(params, dict):
        return []
    out: list[dict] = []
    seen_labels: set[str] = set()
    for key, label, kind in _FIELDS:
        if key not in params or key in _INTERNAL or _secret(key):
            continue
        val = _fmt(kind, params.get(key))
        if val is None:
            continue
        if label in seen_labels:      # «Текст» и «Описание» из двух ключей — один раз
            continue
        seen_labels.add(label)
        out.append({"label": label, "value": val})
    return out


# ── «Повторить с правками» ─────────────────────────────────────────────────
#
# Карточка операции умела «Повторить» — доделать остаток той же работы. Но
# самое частое желание после прогона другое: запустить ТО ЖЕ САМОЕ ещё раз,
# поправив текст, паузу или количество. Раньше для этого надо было найти экран
# запуска и заново выбрать каналы, аккаунты и всё остальное.
#
# Править можно только то, что человек понимает и может проверить глазами:
# текст, числа, паузы, флажки. Списки целей (каналы, аккаунты, люди) копируются
# как есть — их меняют на экране запуска, где есть выбор и проверка.

EDITABLE_KINDS = frozenset({"text", "num", "seconds", "bool"})

# Отметки ПРОГОНА, которые исполнитель дописывает в params по ходу работы. В
# копию они не переходят: по ним исполнитель решил бы, что работа уже сделана
# (launched_bot_ids — «рассылки по этим ботам заведены», broadcast_id — «эта
# рассылка уже идёт», retry_of_op — «смотри журнал предка, там всё отмечено»),
# и копия не сделала бы ничего. Расписание тоже не копируется: копия — один
# запуск, а не вторая цепочка.
CLONE_DROP = frozenset({
    "retry_of_op", "journal_op_ids", "launched_bot_ids", "launched_bc_ids",
    "broadcast_id", "recurring_next_op", "approval_token",
    "repeat_interval_min", "repeat_count",
})

_TEXT_LIMIT = 4096
_NUM_MAX = 1_000_000
_SECONDS_MAX = 86_400


def editable(op_type: str | None, params: Any) -> list[dict]:
    """Поля, которые можно поправить перед копией: [{key,label,kind,value}]."""
    if not isinstance(params, dict):
        return []
    out: list[dict] = []
    for key, label, kind in _FIELDS:
        if kind not in EDITABLE_KINDS or key not in params:
            continue
        if key in _INTERNAL or key in CLONE_DROP or _secret(key):
            continue
        v = params.get(key)
        if kind == "text":
            if not isinstance(v, str):
                continue
        elif kind in ("num", "seconds"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        else:
            v = v is True or str(v).lower() in ("1", "true", "yes")
        out.append({"key": key, "label": label, "kind": kind, "value": v})
    return out


def clone_params(op_type: str | None, params: Any, changes: Any) -> dict:
    """params копии: исходные минус отметки прогона, плюс проверенные правки.

    ValueError с русским текстом — правка не принята (чужой ключ, пустой
    текст, число вне границ). Исходный словарь не меняется.
    """
    base = dict(params) if isinstance(params, dict) else {}
    allowed = {f["key"]: f for f in editable(op_type, base)}
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise ValueError("Правки не разобраны")
    out = {k: v for k, v in base.items()
           if k not in CLONE_DROP and not str(k).startswith("_")}
    for key, val in changes.items():
        f = allowed.get(key)
        if f is None:
            raise ValueError("Это поле у операции не правится")
        kind, label = f["kind"], f["label"]
        if kind == "text":
            text = str(val if val is not None else "").strip()
            if not text:
                raise ValueError(f"«{label}»: не может быть пустым")
            if len(text) > _TEXT_LIMIT:
                raise ValueError(f"«{label}»: длиннее {_TEXT_LIMIT} знаков")
            out[key] = text
        elif kind in ("num", "seconds"):
            try:
                n = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"«{label}»: нужно целое число") from None
            top = _SECONDS_MAX if kind == "seconds" else _NUM_MAX
            if n < 0 or n > top:
                raise ValueError(f"«{label}»: от 0 до {top}")
            out[key] = n
        else:
            out[key] = bool(val is True or str(val).lower() in ("1", "true", "yes"))
    return out
