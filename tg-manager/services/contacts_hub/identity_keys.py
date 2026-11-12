"""Ключи опознания человека в базе контактов: телефон и @ник в сравнимом виде.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ. База контактов сливается из многих источников:
книга аккаунта (Telegram отдаёт номер без «+»), ручной ввод владельца
(«8 (999) 123-45-67»), импорт CSV («+7 999 123 45 67»), CRM, парсер (ник с «@»
или без). Поиск дублей сравнивал телефоны и ники СТРОКОЙ, поэтому
`+79991234567`, `79991234567` и `89991234567` были для него тремя разными
людьми, а `@Ivan` и `ivan` — двумя. Настоящий двойник не попадал ни в одну
группу кандидатов и не показывался на экране «Дубликаты» вообще, а слияние
складывало в карточку три записи одного и того же номера.

Сравнение и группировка обязаны пользоваться ОДНИМ ключом: отбор кандидатов
точен ровно потому, что каждая пара выше порога совпадает хотя бы по одному
ключу группировки (`trust_engine.detect_smart_duplicates`). Разные ключи в
двух местах вернули бы ровно ту ошибку, что уже была с jsonb-телефонами.

Все функции чистые.
"""
from __future__ import annotations

# Меньше семи цифр — не номер, а мусор импорта («0», «-», добавочный «123»):
# такой «телефон» склеил бы в одну группу всех, у кого он записан.
_MIN_DIGITS = 7
_MAX_DIGITS = 15


def phone_key(raw) -> str:
    """Номер в сравнимом виде: только цифры, российская «8» → «7».

    * `+7 (999) 123-45-67`, `79991234567`, `8 999 123 45 67` → `79991234567`;
    * десять цифр, начинающихся с 9 (`9991234567`), — российский мобильный
      без кода страны, так его чаще всего и пишут вручную → `79991234567`;
    * меньше 7 или больше 15 цифр → `''` (не номер; в сравнении не участвует).
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return ""
    return digits


def username_key(raw) -> str:
    """@ник в сравнимом виде: без «@», ссылки t.me и пробелов, нижний регистр."""
    s = str(raw or "").strip()
    low = s.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            s, low = s[len(prefix):], low[len(prefix):]
    for prefix in ("www.", "t.me/", "telegram.me/"):
        if low.startswith(prefix):
            s, low = s[len(prefix):], low[len(prefix):]
    return low.lstrip("@").strip().split("/")[0].split("?")[0]


def phone_keys(phones) -> set[str]:
    """Набор ключей списка телефонов (пустые и мусорные отброшены)."""
    out: set[str] = set()
    for p in phones or []:
        k = phone_key(p)
        if k:
            out.add(k)
    return out


def merge_phones(existing, incoming) -> list:
    """Объединить два списка телефонов без повторов ОДНОГО номера.

    Порядок и написание у уже записанных сохраняются (их выбрал владелец),
    новый номер дописывается, только если его ключа ещё нет. Мусор, которому
    ключ не построить, не теряется: сравнивается строкой, как раньше.
    """
    out = list(existing or [])
    seen = {phone_key(p) or str(p) for p in out}
    for p in incoming or []:
        if p in (None, ""):
            continue
        k = phone_key(p) or str(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def email_key(raw) -> str:
    """Почта в сравнимом виде: нижний регистр, без пробелов; у gmail точки и
    «+метка» в имени не значат ничего (ivan.petrov+shop@gmail.com — тот же
    ящик, что ivanpetrov@gmail.com). Не почта (нет «@» или домена) → ''."""
    s = str(raw or "").strip().lower().replace("mailto:", "")
    if s.count("@") != 1:
        return ""
    local, domain = s.split("@")
    if not local or "." not in domain:
        return ""
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def email_keys(emails) -> set[str]:
    out: set[str] = set()
    for e in emails or []:
        k = email_key(e)
        if k:
            out.add(k)
    return out


def merge_emails(existing, incoming) -> list:
    """Объединить списки почт без повторов одного ящика (см. email_key)."""
    out = list(existing or [])
    seen = {email_key(e) or str(e) for e in out}
    for e in incoming or []:
        if e in (None, ""):
            continue
        k = email_key(e) or str(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out
