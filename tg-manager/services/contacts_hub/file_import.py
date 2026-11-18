"""Импорт баз контактов из файла в хранилище (unified_contacts).

Владелец загружает документ с базой клиентов (CSV / Excel / vCard / текст), где
вперемешку имена, телефоны, почты, адреса и прочее. Файл «грамотно
расшифровывается»: колонки распознаются по заголовкам (рус/англ синонимы), а без
заголовков — по виду данных (где телефоны). Каждая запись ложится в хранилище с
разобранными полями; повтор по телефону — слияние, а не дубль. Потом хранилище
раскладывается по книгам всего флота существующим синком (op contacts_sync).

Разбор — чистые функции (parse_contacts), без базы и сети: тестируются на месте.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid

log = logging.getLogger(__name__)

MAX_ROWS = 100_000

# Синонимы заголовков колонок (в нижнем регистре, рус + англ).
_H = {
    "first_name": ("имя", "first", "firstname", "first name", "имя контакта"),
    "last_name": ("фамилия", "last", "surname", "lastname", "last name"),
    "patronymic": ("отчество", "patronymic", "middle name", "middle"),
    "name": ("фио", "имя фамилия", "name", "full name", "контакт", "клиент",
             "client", "customer", "получатель"),
    "phone": ("телефон", "тел", "phone", "номер", "mobile", "моб", "tel",
              "msisdn", "cell", "phone number", "номер телефона", "контактный"),
    "email": ("почта", "email", "e-mail", "mail", "емейл", "эл. почта", "e mail"),
    "address": ("адрес", "address", "город", "city", "addr", "адрес доставки"),
    "company": ("компания", "company", "организация", "org", "фирма", "ооо"),
    "notes": ("заметк", "notes", "note", "комментар", "comment", "инфо", "info",
              "примечан", "описание"),
}

_PHONE_RE = re.compile(r"\+?\d[\d\s\-\(\)]{7,}\d")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def norm_phone(raw: str) -> str:
    """Телефон → +7XXXXXXXXXX / цифры. Пусто, если это не номер."""
    s = (raw or "").strip()
    if not s:
        return ""
    plus = s.lstrip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 10 or len(digits) > 15:
        return ""
    # Российские 8XXXXXXXXXX → +7XXXXXXXXXX; 10 цифр без кода → как есть.
    if not plus and len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return ("+" + digits) if (plus or len(digits) >= 11) else digits


def _match_header(cell: str) -> str | None:
    c = (cell or "").strip().lower()
    if not c:
        return None
    for field, syns in _H.items():
        for s in syns:
            if c == s or c.startswith(s):
                return field
    return None


def _looks_like_header(row: list[str]) -> dict[int, str]:
    """{индекс колонки → поле}, если строка похожа на заголовки."""
    out: dict[int, str] = {}
    for i, c in enumerate(row):
        f = _match_header(str(c))
        if f:
            out[i] = f
    # Заголовок, только если распознали хотя бы телефон или имя.
    if any(v in ("phone", "name", "first_name") for v in out.values()):
        return out
    return {}


def _row_to_contact(cells: list[str], colmap: dict[int, str]) -> dict:
    """Строка таблицы → контакт по карте колонок (или эвристике)."""
    rec = {"phones": [], "emails": [], "addresses": [], "first_name": "",
           "last_name": "", "patronymic": "", "company": "", "notes": "",
           "display_name": ""}
    name_parts = []
    for i, cell in enumerate(cells):
        val = str(cell or "").strip()
        if not val:
            continue
        field = colmap.get(i)
        if field == "phone":
            p = norm_phone(val)
            if p and p not in rec["phones"]:
                rec["phones"].append(p)
        elif field == "email":
            for m in _EMAIL_RE.findall(val):
                if m not in rec["emails"]:
                    rec["emails"].append(m)
        elif field == "address":
            rec["addresses"].append(val)
        elif field == "company":
            rec["company"] = val
        elif field == "notes":
            rec["notes"] = (rec["notes"] + " " + val).strip()
        elif field == "first_name":
            rec["first_name"] = val
        elif field == "last_name":
            rec["last_name"] = val
        elif field == "patronymic":
            rec["patronymic"] = val
        elif field == "name":
            name_parts.append(val)
        else:
            # Колонка без карты: вытащить телефон/почту, иначе — в имя/заметку.
            p = norm_phone(val)
            if p:
                if p not in rec["phones"]:
                    rec["phones"].append(p)
                continue
            em = _EMAIL_RE.findall(val)
            if em:
                for m in em:
                    if m not in rec["emails"]:
                        rec["emails"].append(m)
                continue
            if not colmap and not name_parts and not any(ch.isdigit() for ch in val):
                name_parts.append(val)
            elif val:
                rec["notes"] = (rec["notes"] + " " + val).strip()
    # Имя из "name" колонок, если нет отдельных first/last.
    full = " ".join(name_parts).strip()
    if full and not rec["first_name"] and not rec["last_name"]:
        bits = full.split()
        rec["last_name"] = bits[0] if len(bits) > 1 else ""
        rec["first_name"] = bits[1] if len(bits) > 1 else bits[0] if bits else ""
        if len(bits) > 2:
            rec["patronymic"] = bits[2]
    rec["display_name"] = (full or " ".join(
        x for x in (rec["last_name"], rec["first_name"], rec["patronymic"]) if x)).strip()
    return rec


def _parse_table(rows: list[list[str]]) -> list[dict]:
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        return []
    colmap = _looks_like_header(rows[0])
    body = rows[1:] if colmap else rows
    out = []
    for r in body[:MAX_ROWS]:
        rec = _row_to_contact(r, colmap)
        if rec["phones"] or rec["display_name"] or rec["emails"]:
            out.append(rec)
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _parse_csv(data: bytes) -> list[dict]:
    text = _decode(data)
    sample = text[:4096]
    delim = ","
    for d in (";", "\t", ","):
        if sample.count(d) >= sample.count(delim):
            delim = d
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        return _parse_table([list(r) for r in reader])
    except Exception as e:
        log.debug("file_import: csv parse: %s", e)
        return _parse_text(data)


def _parse_xlsx(data: bytes) -> list[dict]:
    try:
        from openpyxl import load_workbook
    except Exception:
        return []
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else str(c)) for c in row]
                for row in ws.iter_rows(values_only=True)]
        wb.close()
        return _parse_table(rows)
    except Exception as e:
        log.debug("file_import: xlsx parse: %s", e)
        return []


def _parse_vcard(data: bytes) -> list[dict]:
    text = _decode(data)
    out = []
    for block in re.split(r"(?i)BEGIN:VCARD", text):
        if "END:VCARD" not in block.upper() and "TEL" not in block.upper():
            continue
        rec = {"phones": [], "emails": [], "addresses": [], "first_name": "",
               "last_name": "", "patronymic": "", "company": "", "notes": "",
               "display_name": ""}
        for line in block.splitlines():
            up = line.upper()
            val = line.split(":", 1)[1].strip() if ":" in line else ""
            if up.startswith("TEL"):
                p = norm_phone(val)
                if p and p not in rec["phones"]:
                    rec["phones"].append(p)
            elif up.startswith("EMAIL"):
                if val and val not in rec["emails"]:
                    rec["emails"].append(val)
            elif up.startswith("ADR"):
                a = val.replace(";", " ").strip()
                if a:
                    rec["addresses"].append(a)
            elif up.startswith("ORG"):
                rec["company"] = val.replace(";", " ").strip()
            elif up.startswith("FN"):
                rec["display_name"] = val
            elif up.startswith("N:") or up.startswith("N;"):
                bits = [b.strip() for b in val.split(";") if b.strip()]
                if bits:
                    rec["last_name"] = bits[0]
                    if len(bits) > 1:
                        rec["first_name"] = bits[1]
                    if len(bits) > 2:
                        rec["patronymic"] = bits[2]
        if not rec["display_name"]:
            rec["display_name"] = " ".join(
                x for x in (rec["last_name"], rec["first_name"]) if x).strip()
        if rec["phones"] or rec["display_name"] or rec["emails"]:
            out.append(rec)
    return out


def _parse_text(data: bytes) -> list[dict]:
    """Свободный текст: каждая строка с телефоном — контакт; текст рядом — имя."""
    text = _decode(data)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        phones = [norm_phone(m) for m in _PHONE_RE.findall(line)]
        phones = [p for p in phones if p]
        emails = _EMAIL_RE.findall(line)
        if not phones and not emails:
            continue
        name = _PHONE_RE.sub(" ", line)
        name = _EMAIL_RE.sub(" ", name)
        name = re.sub(r"[,;:\t]+", " ", name).strip()
        rec = {"phones": list(dict.fromkeys(phones)),
               "emails": list(dict.fromkeys(emails)),
               "addresses": [], "first_name": "", "last_name": "",
               "patronymic": "", "company": "", "notes": "", "display_name": name}
        if name:
            bits = name.split()
            rec["last_name"] = bits[0] if len(bits) > 1 else ""
            rec["first_name"] = bits[1] if len(bits) > 1 else (bits[0] if bits else "")
        out.append(rec)
    return out


def parse_contacts(filename: str, data: bytes) -> list[dict]:
    """Разобрать файл базы контактов в список записей. Формат — по расширению,
    с фолбэком на текст/CSV."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        rows = _parse_xlsx(data)
        if rows:
            return rows
    if name.endswith(".vcf") or b"BEGIN:VCARD" in data[:64].upper():
        rows = _parse_vcard(data)
        if rows:
            return rows
    if name.endswith((".csv", ".tsv")):
        rows = _parse_csv(data)
        if rows:
            return rows
    if name.endswith(".txt"):
        # .txt — это обычно свободный список «имя + номер» построчно, где CSV-
        # разбор склеил бы имя с номером в одну ячейку. Но таб/точка-с-запятой
        # означают настоящие колонки — тогда это CSV.
        head = _decode(data[:4096])
        if "\t" in head or ";" in head:
            rows = _parse_csv(data)
            if rows:
                return rows
        rows = _parse_text(data)
        if rows:
            return rows
    # Неизвестное расширение — пробуем по содержимому.
    for fn in (_parse_csv, _parse_text):
        rows = fn(data)
        if rows:
            return rows
    return []


async def import_contacts(pool, owner_id: int, rows: list[dict], source: str = "файл") -> dict:
    """Положить записи в хранилище. Дедуп по телефону: совпал — слияние (поля
    дополняются, телефоны/почты/адреса объединяются), иначе — новая запись.
    Возвращает {created, updated, total, skipped}."""
    created = updated = skipped = 0
    tag = source[:40]
    for rec in rows:
        phones = [p for p in rec.get("phones", []) if p]
        if not phones and not (rec.get("display_name") or rec.get("emails")):
            skipped += 1
            continue
        existing = None
        if phones:
            try:
                existing = await pool.fetchrow(
                    "SELECT id, phones, emails, addresses, first_name, last_name, "
                    "patronymic, display_name, company, notes, tags "
                    "FROM unified_contacts WHERE owner_id=$1 AND phones ?| $2::text[] LIMIT 1",
                    owner_id, phones)
            except Exception as e:
                log.debug("file_import: lookup: %s", e)
        try:
            if existing:
                await _merge(pool, owner_id, existing, rec, tag)
                updated += 1
            else:
                await _insert(pool, owner_id, rec, tag)
                created += 1
        except Exception as e:
            log.warning("file_import: запись контакта owner=%s: %s", owner_id, e)
            skipped += 1
    return {"created": created, "updated": updated,
            "total": len(rows), "skipped": skipped}


def _union(old, new):
    out = list(old or [])
    for x in (new or []):
        if x not in out:
            out.append(x)
    return out


async def _insert(pool, owner_id, rec, tag):
    await pool.execute(
        "INSERT INTO unified_contacts (id, owner_id, first_name, last_name, "
        "patronymic, display_name, phones, emails, addresses, company, notes, "
        "tags, persona_type) VALUES "
        "($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11,$12,'imported')",
        str(uuid.uuid4()), owner_id, rec.get("first_name") or None,
        rec.get("last_name") or None, rec.get("patronymic") or None,
        rec.get("display_name") or None, json.dumps(rec.get("phones", [])),
        json.dumps(rec.get("emails", [])), json.dumps(rec.get("addresses", [])),
        rec.get("company") or None, rec.get("notes") or "", [tag, "импорт"])


async def _merge(pool, owner_id, existing, rec, tag):
    def _parse(v):
        if isinstance(v, list):
            return v
        try:
            return json.loads(v) if v else []
        except (ValueError, TypeError):
            return []
    phones = _union(_parse(existing["phones"]), rec.get("phones"))
    emails = _union(_parse(existing["emails"]), rec.get("emails"))
    addrs = _union(_parse(existing["addresses"]), rec.get("addresses"))
    tags = _union(list(existing["tags"] or []), [tag, "импорт"])
    await pool.execute(
        "UPDATE unified_contacts SET "
        "first_name=COALESCE(NULLIF(first_name,''), $3), "
        "last_name=COALESCE(NULLIF(last_name,''), $4), "
        "patronymic=COALESCE(NULLIF(patronymic,''), $5), "
        "display_name=COALESCE(NULLIF(display_name,''), $6), "
        "company=COALESCE(NULLIF(company,''), $7), "
        "notes=NULLIF(TRIM(CONCAT(notes, ' ', $8::text)), ''), "
        "phones=$9::jsonb, emails=$10::jsonb, addresses=$11::jsonb, "
        "tags=$12, updated_at=NOW() WHERE id=$1 AND owner_id=$2",
        existing["id"], owner_id, rec.get("first_name") or None,
        rec.get("last_name") or None, rec.get("patronymic") or None,
        rec.get("display_name") or None, rec.get("company") or None,
        rec.get("notes") or "", json.dumps(phones), json.dumps(emails),
        json.dumps(addrs), tags)
