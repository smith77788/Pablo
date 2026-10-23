"""Разбор списка для инвайта из файла: .txt / .csv / .xlsx / .db + снятие лимита 500."""
from __future__ import annotations

import io
import sqlite3
import tempfile
import os

import pytest

from services import invite_list_parser as ilp
from services.mass_inviter_engine import parse_user_refs, parse_phones


def test_txt_refs():
    data = "@user1, @user2\n123456789\n@user3".encode()
    refs = ilp.parse_invite_file("list.txt", data, mode="refs")
    assert refs == ["@user1", "@user2", "123456789", "@user3"]


def test_txt_phones():
    data = b"+79991234567\n8 999 111 22 33\n+1 202 555 0143"
    phones = ilp.parse_invite_file("nums.txt", data, mode="phones")
    assert "+79991234567" in phones
    assert "+89991112233" in phones
    assert len(phones) == 3


def test_csv_refs():
    data = b"id,username\n111,@alpha\n222,@beta\n333,gamma"
    refs = ilp.parse_invite_file("a.csv", data, mode="refs")
    # реальные пользователи распознаны (заголовок «username» может дать безвредный
    # лишний токен — на резолве он просто отсеется, дубликаты не страшны)
    assert {"111", "@alpha", "222", "@beta", "333", "@gamma"}.issubset(set(refs))


def test_xlsx_refs():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["username", "id"])
    ws.append(["@vasya", 100200300])
    ws.append(["petya", None])
    buf = io.BytesIO()
    wb.save(buf)
    refs = ilp.parse_invite_file("book.xlsx", buf.getvalue(), mode="refs")
    assert "@vasya" in refs and "100200300" in refs and "@petya" in refs


def test_sqlite_db_refs():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE users(id INTEGER, username TEXT, phone TEXT)")
        con.executemany("INSERT INTO users VALUES(?,?,?)", [
            (555000111, "durov", "+79990001122"),
            (555000222, "telegram", None),
        ])
        con.commit()
        con.close()
        with open(path, "rb") as f:
            data = f.read()
    finally:
        os.unlink(path)
    refs = ilp.parse_invite_file("audience.db", data, mode="refs")
    assert "@durov" in refs and "555000111" in refs and "@telegram" in refs
    phones = ilp.parse_invite_file("audience.db", data, mode="phones")
    assert "+79990001122" in phones


def test_empty_and_bad():
    with pytest.raises(ValueError):
        ilp.parse_invite_file("x.txt", b"", mode="refs")
    # битый xlsx → понятная ошибка, не краш
    with pytest.raises(ValueError):
        ilp.parse_invite_file("broken.xlsx", b"not a real xlsx", mode="refs")


def test_limit_raised_above_500():
    # генерируем 1200 юзеров — раньше срезалось до 500, теперь принимаем всё
    big = "\n".join(f"@user{i:05d}" for i in range(1200))
    refs = parse_user_refs(big)
    assert len(refs) == 1200
    nums = "\n".join(f"+7999{i:07d}" for i in range(1200))
    phones = parse_phones(nums)
    assert len(phones) == 1200


def test_explicit_limit_still_respected():
    big = "\n".join(f"@user{i:05d}" for i in range(100))
    assert len(parse_user_refs(big, limit=10)) == 10
