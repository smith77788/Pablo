"""Разбор списка для инвайта из файла: .txt / .csv / .xlsx / .db (SQLite).

Оператор часто держит аудиторию не текстом в сообщении, а файлом: выгрузка
парсера в .txt/.csv, таблица Excel, или SQLite-база (.db) от стороннего скрейпера.
Здесь — извлечение всех значений в один текст, который затем растаскивают
parse_user_refs / parse_phones (те же правила, что и для ручного ввода).

Чистые функции (байты → список) — тестируются без Telegram/бота.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import tempfile

from services.mass_inviter_engine import parse_user_refs, parse_phones

log = logging.getLogger(__name__)

# Лимит на размер файла (защита памяти). 25 МБ хватает на сотни тысяч строк.
MAX_FILE_BYTES = 25 * 1024 * 1024


def _text_from_csv(data: bytes) -> str:
    """CSV → плоский текст (все ячейки через перенос). Разделители parse_* съест."""
    text = data.decode("utf-8", errors="replace")
    out: list[str] = []
    try:
        for row in csv.reader(io.StringIO(text)):
            out.extend(str(c) for c in row if c)
    except Exception:
        return text  # не распарсился как CSV — вернём как есть
    return "\n".join(out) if out else text


def _text_from_xlsx(data: bytes) -> str:
    """XLSX → плоский текст всех непустых ячеек всех листов (read-only)."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell is not None:
                        out.append(str(cell))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return "\n".join(out)


def _text_from_sqlite(data: bytes) -> str:
    """SQLite (.db) → плоский текст всех значений всех пользовательских таблиц.

    Не знаем схему стороннего экспорта, поэтому берём ВСЕ значения — parse_*
    выберет из них то, что похоже на @username / id / телефон.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    out: list[str] = []
    try:
        os.write(fd, data)
        os.close(fd)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                try:
                    cur.execute(f'SELECT * FROM "{t}"')
                    for row in cur.fetchall():
                        for v in row:
                            if v is not None:
                                out.append(str(v))
                except Exception:
                    continue
        finally:
            con.close()
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    return "\n".join(out)


def extract_text(filename: str, data: bytes) -> str:
    """Достать плоский текст из файла по расширению (fallback — как текст)."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _text_from_xlsx(data)
    if name.endswith((".db", ".sqlite", ".sqlite3")):
        return _text_from_sqlite(data)
    if name.endswith(".csv"):
        return _text_from_csv(data)
    # .txt и всё остальное — как обычный текст
    return data.decode("utf-8", errors="replace")


def parse_invite_file(filename: str, data: bytes, mode: str = "refs") -> list[str]:
    """Файл → список (@username/id при mode='refs', телефоны при mode='phones').

    Бросает ValueError с понятным текстом на пустой/битый/слишком большой файл.
    """
    if not data:
        raise ValueError("файл пустой")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"файл слишком большой (>{MAX_FILE_BYTES // (1024 * 1024)} МБ)")
    try:
        text = extract_text(filename, data)
    except Exception as e:
        log.warning("invite_list_parser: не разобрал %s: %s", filename, e)
        raise ValueError("не удалось прочитать файл (формат не поддержан или повреждён)")
    if mode == "phones":
        return parse_phones(text)
    return parse_user_refs(text)
