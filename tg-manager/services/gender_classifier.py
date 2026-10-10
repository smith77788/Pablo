"""Определение пола по имени (RU/UA) — эвристика для таргетинга рассылок.

Это ОЦЕНКА, не факт: сначала словарь частых имён (надёжно), затем эвристика
окончаний с явными списками исключений (имена на -а/-я бывают мужскими: Никита,
Илья; на согласную — женскими: Любовь, Адель). Неоднозначное → None ('неизвестно'),
чтобы не приписывать пол наугад (лучше «неизвестно», чем неверный таргет).

classify() детерминирована и не ходит в сеть/БД — тестируется юнитом.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

# Частые мужские имена (RU/UA + распространённые уменьшительные).
_MALE = {
    "александр", "саша", "сашко", "алексей", "олексій", "лёша", "андрей", "андрій",
    "антон", "аркадий", "артём", "артем", "артур", "богдан", "борис", "вадим",
    "валентин", "валерий", "василий", "василь", "виктор", "віктор", "виталий",
    "віталій", "владимир", "володимир", "вова", "владислав", "влад", "вячеслав",
    "геннадий", "георгий", "глеб", "григорий", "григорій", "давид", "данил",
    "данила", "даниил", "данило", "денис", "дмитрий", "дмитро", "дима", "евгений",
    "євген", "женя", "егор", "єгор", "иван", "іван", "ваня", "игорь", "ігор",
    "илья", "ілля", "кирилл", "кирило", "константин", "костянтин", "костя", "лев",
    "леонид", "леонід", "макар", "максим", "марк", "матвей", "матвій", "михаил",
    "михайло", "миша", "назар", "никита", "нікіта", "николай", "микола", "коля",
    "олег", "остап", "павел", "павло", "паша", "пётр", "петр", "петро", "роман",
    "рома", "ростислав", "руслан", "святослав", "семён", "семен", "сергей",
    "сергій", "серёжа", "станислав", "станіслав", "стас", "степан", "тарас",
    "тимофей", "тимур", "фёдор", "федор", "федір", "филипп", "юрий", "юрій", "юра",
    "ярослав", "кузьма", "фома", "лука", "савва", "нестор", "влас",
    # унисекс-уменьшительные — дублируются в _FEMALE: classify вернёт None
    "женя", "валя", "сима",
}

# Частые женские имена.
_FEMALE = {
    "александра", "саша", "алина", "аліна", "алла", "анастасия", "анастасія",
    "настя", "ангелина", "анна", "аня", "антонина", "валентина", "валерия",
    "валерія", "варвара", "вера", "віра", "вероника", "вероніка", "виктория",
    "вікторія", "вика", "галина", "галя", "дарья", "дарія", "даша", "диана",
    "діана", "евгения", "євгенія", "екатерина", "катерина", "катя", "елена",
    "олена", "лена", "елизавета", "єлизавета", "лиза", "жанна", "зоя", "инна",
    "інна", "ирина", "ірина", "ира", "карина", "каріна", "кристина", "христина",
    "ксения", "ксенія", "ксюша", "лариса", "лидия", "лідія", "лилия", "лілія",
    "любовь", "любов", "люба", "людмила", "мила", "маргарита", "марина", "мария",
    "марія", "маша", "надежда", "надія", "надя", "наталья", "наталія", "наташа",
    "нина", "ніна", "оксана", "олеся", "ольга", "оля", "полина", "поліна",
    "раиса", "регина", "світлана", "светлана", "света", "снежана", "софия",
    "софія", "соня", "тамара", "татьяна", "тетяна", "таня", "ульяна", "уляна",
    "юлия", "юлія", "юля", "яна", "ярослава", "адель", "аліна", "нинель", "айгуль",
    "гульнара", "эльвира", "ельвіра",
    # унисекс-уменьшительные — присутствуют и в _MALE: classify вернёт None
    "женя", "валя", "сима",
}

# Мужские имена, оканчивающиеся на -а/-я (эвристика окончаний их бы спутала).
_MALE_VOWEL_END = {
    "никита", "нікіта", "илья", "ілля", "данила", "данило", "кузьма", "фома",
    "лука", "савва", "жора", "гоша", "дима", "вова", "коля", "юра", "паша",
    "саша", "миша", "гриша", "лёша", "рома", "серёжа", "ваня", "петя", "витя",
    "толя", "боря",
}

# Женские имена, оканчивающиеся на согласную (эвристика сочла бы мужскими).
_FEMALE_CONS_END = {
    "любовь", "любов", "нинель", "адель", "айгуль", "гульнара", "эсфирь", "юдифь",
}


def _norm(name: str | None) -> str:
    if not name:
        return ""
    # первый токен, буквы/дефис, нижний регистр; ё→е для единообразия словаря
    token = name.strip().split()[0] if name.strip() else ""
    token = "".join(ch for ch in token.lower() if ch.isalpha() or ch == "-")
    return token


def classify(first_name: str | None, last_name: str | None = None) -> str | None:
    """Вернуть 'm' | 'f' | None (неизвестно) по имени.

    Порядок: точный словарь → списки исключений окончаний → эвристика окончаний.
    Имена-омонимы (напр. 'Саша' есть и в мужском, и в женском) → None: без
    доп. сигнала не угадываем.
    """
    name = _norm(first_name)
    if not name:
        return None

    in_male = name in _MALE
    in_female = name in _FEMALE
    if in_male and in_female:
        return None  # унисекс-уменьшительное (Саша/Женя) — не гадаем
    if in_male:
        return "m"
    if in_female:
        return "f"

    # Явные исключения окончаний
    if name in _MALE_VOWEL_END:
        return "m"
    if name in _FEMALE_CONS_END:
        return "f"

    # Эвристика окончаний (RU/UA): -а/-я обычно женское, согласная — мужское.
    if name.endswith(("а", "я")):
        return "f"
    if name.endswith("й"):
        return "m"
    # оканчивается на согласную (кроме мягкого/твёрдого знака ь/ъ — там пол
    # неоднозначен: Игорь=м, Любовь=ж, поэтому такие → None) → скорее мужское
    if name[-1] in "бвгджзклмнпрстфхцчшщ":
        return "m"
    return None


# Метки для UI
LABELS = {"m": "👨 Муж", "f": "👩 Жен", None: "❔ Неизв"}


def label(g: str | None) -> str:
    return LABELS.get(g, "❔ Неизв")


async def classify_audience(
    pool: asyncpg.Pool, owner_id: int, run_id: int | None = None
) -> dict:
    """Разметить пол у участников аудитории (всей или одной базы) по имени.

    Чистая БД+CPU операция (без Telegram): синхронно. Пишет parsed_audiences.gender
    одним UPDATE через unnest. Идемпотентна: повторный прогон даёт тот же результат.
    Возвращает разбивку {'m','f','unknown','total'}.
    """
    where = "owner_id=$1" + (" AND parse_run_id=$2" if run_id else "")
    args = [owner_id] + ([run_id] if run_id else [])
    rows = await pool.fetch(
        f"SELECT id, first_name, last_name FROM parsed_audiences WHERE {where}", *args)

    ids: list[int] = []
    genders: list[str | None] = []
    counts = {"m": 0, "f": 0, "unknown": 0}
    for r in rows:
        g = classify(r["first_name"], r["last_name"])
        ids.append(r["id"])
        genders.append(g)
        counts["unknown" if g is None else g] += 1

    if ids:
        # Один UPDATE ... FROM unnest(...): без N запросов и без риска биндинга
        # по одному (проверяется на живой БД в тесте).
        await pool.execute(
            "UPDATE parsed_audiences AS p SET gender = v.g "
            "FROM (SELECT unnest($1::bigint[]) AS id, unnest($2::text[]) AS g) v "
            "WHERE p.id = v.id AND p.owner_id = $3",
            ids, genders, owner_id,
        )
    log.info("gender.classify owner=%s run=%s → m=%d f=%d unk=%d",
             owner_id, run_id, counts["m"], counts["f"], counts["unknown"])
    return {"m": counts["m"], "f": counts["f"],
            "unknown": counts["unknown"], "total": len(ids)}


async def classify_contacts(
    pool: asyncpg.Pool, owner_id: int, only_missing: bool = True
) -> dict:
    """Разметить пол у контактов Хаба (unified_contacts) по имени. Аналог
    classify_audience, но по UUID-контактам. only_missing=True — только те, у кого
    gender ещё NULL (дёшево на большом хабе); False — переразметить всё.

    Чистая БД+CPU, без Telegram. Один UPDATE через unnest. Идемпотентна.
    """
    where = "owner_id=$1" + (" AND gender IS NULL" if only_missing else "")
    rows = await pool.fetch(
        f"SELECT id, first_name, last_name FROM unified_contacts WHERE {where}",
        owner_id)
    ids: list = []
    genders: list[str | None] = []
    counts = {"m": 0, "f": 0, "unknown": 0}
    for r in rows:
        g = classify(r["first_name"], r["last_name"])
        ids.append(r["id"])
        genders.append(g)
        counts["unknown" if g is None else g] += 1
    if ids:
        await pool.execute(
            "UPDATE unified_contacts AS u SET gender = v.g "
            "FROM (SELECT unnest($1::uuid[]) AS id, unnest($2::text[]) AS g) v "
            "WHERE u.id = v.id AND u.owner_id = $3",
            ids, genders, owner_id,
        )
    log.info("gender.classify_contacts owner=%s → m=%d f=%d unk=%d (of %d)",
             owner_id, counts["m"], counts["f"], counts["unknown"], len(ids))
    return {"m": counts["m"], "f": counts["f"],
            "unknown": counts["unknown"], "total": len(ids)}

