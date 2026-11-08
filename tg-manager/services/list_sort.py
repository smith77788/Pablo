"""Сортировка списков ресурсов: аккаунты, боты, каналы, прокси.

Списки отдавались в одном зашитом порядке. При десятке ресурсов это не
мешает, при тысяче — да: чтобы найти худшие по траст-скору аккаунты или самые
медленные прокси, их приходилось листать страницами. Здесь — белый список
ключей сортировки на каждый вид ресурса. Из запроса в SQL попадает только
ключ из этого списка: ни имя колонки, ни направление не подставляются как
текст из запроса.

Каждый порядок заканчивается стабильным ключом (id), иначе страницы по
offset могли бы терять и дублировать строки при равных значениях.
"""
from __future__ import annotations

# вид → ключ → (SQL-выражение, направление по умолчанию, подпись для экрана).
# Выражения пишутся под тот SELECT, в котором используются (алиасы колонок).
SORTS: dict[str, dict[str, tuple[str, str, str]]] = {
    "accounts": {
        "default": ("is_active DESC, last_used DESC NULLS LAST", "", "Сначала рабочие"),
        "status": (
            "CASE WHEN COALESCE(acc_status,'ok') IN ('banned','deactivated','session_expired') THEN 0 "
            "WHEN COALESCE(acc_status,'ok')='spamblock' THEN 1 "
            "WHEN cooldown_until IS NOT NULL AND cooldown_until > now() THEN 2 "
            "WHEN NOT is_active THEN 3 ELSE 4 END",
            "ASC", "Сначала проблемные"),
        "trust": ("COALESCE(trust_score, 1.0)", "ASC", "Траст-скор"),
        "added": ("added_at", "DESC", "Дата добавления"),
        "last_used": ("last_used", "DESC", "Последняя работа"),
        "phone": ("phone", "ASC", "Телефон"),
        "name": ("LOWER(COALESCE(first_name, username, ''))", "ASC", "Имя"),
        "geo": ("COALESCE((SELECT UPPER(p.geo_country) FROM user_proxies p "
                "WHERE p.id = tg_accounts.proxy_id), 'ZZ')", "ASC", "Страна"),
        "pool": ("COALESCE(pool, '')", "ASC", "Пул"),
        "stage": ("COALESCE(stage, 'new')", "ASC", "Статус CRM"),
    },
    "bots": {
        "default": ("subscriber_count DESC", "", "Подписчики"),
        "users": ("total_users", "DESC", "Все пользователи"),
        # Запрос ботов — SELECT DISTINCT: ORDER BY там принимает только
        # колонки из списка выборки, поэтому здесь голые колонки.
        "name": ("mb.username", "ASC", "Имя"),
        "status": ("mb.fail_streak", "DESC", "Сначала со сбоями"),
        "added": ("mb.bot_id", "DESC", "Сначала новые"),
    },
    "channels": {
        "default": ("member_count DESC NULLS LAST", "", "Участники"),
        "title": ("LOWER(COALESCE(title, username, ''))", "ASC", "Название"),
        "added": ("added_at", "DESC", "Дата добавления"),
        "role": ("CASE WHEN is_creator THEN 0 WHEN is_admin THEN 1 ELSE 2 END", "ASC", "Сначала мои"),
    },
    "proxies": {
        "default": ("created_at DESC", "", "Сначала новые"),
        "alive": ("CASE WHEN is_alive IS FALSE THEN 0 WHEN is_alive IS NULL THEN 1 ELSE 2 END",
                  "ASC", "Сначала мёртвые"),
        "latency": ("latency_avg_ms", "DESC", "Задержка"),
        "accounts": ("(SELECT COUNT(*) FROM tg_accounts a WHERE a.proxy_id = user_proxies.id)",
                     "DESC", "Сколько аккаунтов"),
        "label": ("LOWER(COALESCE(label, ''))", "ASC", "Метка"),
    },
}

# Стабильный последний ключ: у каждого вида свой столбец id.
_TIEBREAK = {"accounts": "id", "bots": "mb.bot_id", "channels": "channel_id",
             "proxies": "id"}


def order_by(kind: str, sort: str | None, direction: str | None = None) -> str:
    """Готовое тело ORDER BY для вида ресурса. Неизвестный ключ — порядок по
    умолчанию; направление — только ASC или DESC."""
    table = SORTS[kind]
    if not sort or sort not in table or sort == "default":
        return f"{table['default'][0]}, {_TIEBREAK[kind]}"
    expr, default_dir, _ = table[sort]
    d = (direction or "").upper()
    if d not in ("ASC", "DESC"):
        d = default_dir
    return f"{expr} {d} NULLS LAST, {_TIEBREAK[kind]}"


def options(kind: str) -> list[dict]:
    """Ключи сортировки для экрана: [{key, label, dir}]."""
    return [{"key": k, "label": v[2], "dir": v[1] or "DESC"}
            for k, v in SORTS[kind].items()]
