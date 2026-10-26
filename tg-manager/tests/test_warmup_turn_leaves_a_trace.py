"""Разогрев чата перестал быть немым: каждый ход оставляет след.

ЧТО ЛОМАЛОСЬ. У разогрева снаружи был ровно один показатель — счётчик
`messages_sent` в строке списка, и саму строку нельзя было даже открыть. Поэтому
на вопрос «почему флот молчит» ответа не существовало в принципе: владелец видел
«активен · смешанный · отправлено 0» и всё.

А промолчать ход может по десятку причин, и каждая из них была голым `return`
без единой записи: в сессии нет аккаунтов, подходящий не выбрался, аккаунт
выключен или на паузе Telegram, аккаунт в карантине по здоровью, темп снижен
губернатором или ночью, аккаунт занят операцией, флот НЕ СМОГ ВОЙТИ В ЧАТ, в
чате нет новых сообщений, генератор ответов недоступен, текст отклонён проверкой
контента, Telegram попросил паузу. Последние — не мелочь: пока флот не в чате,
«активен · отправлено 0» держится вечно и выглядит как поломка продукта.

Это ещё и нарушение жёсткого гейта: массовая операция по живым аккаунтам обязана
иметь per-target лог и честный счётчик. Разогрев — массовая операция, и журнала
у неё не было вовсе.

ЧТО ТЕПЕРЬ. Каждый ход пишется в chat_warmup_log: удачный (что написали и кому),
пропущенный (с причиной по-русски) и сорвавшийся. Подряд идущие пропуски с одной
причиной не плодят строки — у последней растёт `repeats`. Экран «Ход разогрева»
показывает сводку, вклад каждого аккаунта и журнал.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

from services import chat_warmup as cw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _fn(name: str, module: str = "chat_warmup") -> ast.AST:
    src = _read("services", f"{module}.py")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция {name} не найдена")


# ── Заглушка пула ────────────────────────────────────────────────────────────


class _Pool:
    def __init__(self, prev=None, raises: bool = False):
        self._prev = prev
        self._raises = raises
        self.inserted: list[tuple] = []
        self.updated: list[tuple] = []

    async def fetchrow(self, sql, *args):
        if self._raises:
            raise RuntimeError("БД недоступна")
        return self._prev

    async def execute(self, sql, *args):
        if self._raises:
            raise RuntimeError("БД недоступна")
        if sql.strip().startswith("INSERT"):
            self.inserted.append((sql, args))
        else:
            self.updated.append((sql, args))
        return "INSERT 0 1"


SESSION = {"id": 7, "owner_id": 42}


# ── Писатель журнала ─────────────────────────────────────────────────────────


def test_a_sent_message_is_written_down():
    pool = _Pool()
    asyncio.run(cw._log(pool, SESSION, "ok", kind="reply", text="привет",
                        account_id=5, reply_to="Лора: как дела"))

    assert pool.inserted, "удачный ход не попал в журнал"
    sql, args = pool.inserted[0]
    assert "chat_warmup_log" in sql
    assert 7 in args and 42 in args and 5 in args
    assert "привет" in args, "в журнале нет того, что аккаунт написал"
    assert "Лора: как дела" in args, "не видно, кому отвечали"


def test_a_silent_turn_says_why():
    pool = _Pool()
    asyncio.run(cw._log(pool, SESSION, "skipped", "Аккаунт в карантине по здоровью",
                        account_id=5))

    assert pool.inserted, "пропущенный ход нигде не отметился"
    assert any("карантин" in str(a) for a in pool.inserted[0][1])


def test_the_same_reason_twice_does_not_grow_the_table():
    # Сессия, которая сутки не может войти в чат, иначе написала бы тысячи
    # одинаковых строк, а владельцу нужно одно: что мешает и давно ли.
    prev = {"id": 11, "status": "error", "reason": "Не удалось войти в чат"}
    pool = _Pool(prev=prev)
    asyncio.run(cw._log(pool, SESSION, "error", "Не удалось войти в чат", account_id=5))

    assert not pool.inserted, "повтор той же причины добавил новую строку"
    assert pool.updated, "счётчик повторов не вырос"
    sql, args = pool.updated[0]
    assert "repeats = repeats + 1" in sql and "created_at = NOW()" in sql
    assert 11 in args


def test_a_new_reason_starts_a_new_line():
    prev = {"id": 11, "status": "skipped", "reason": "Новых сообщений в чате нет"}
    pool = _Pool(prev=prev)
    asyncio.run(cw._log(pool, SESSION, "error", "Не удалось войти в чат", account_id=5))
    assert pool.inserted, "смена причины обязана быть видна в журнале отдельной строкой"


def test_a_successful_turn_is_never_collapsed():
    prev = {"id": 11, "status": "ok", "reason": ""}
    pool = _Pool(prev=prev)
    asyncio.run(cw._log(pool, SESSION, "ok", kind="seed", text="вторая реплика",
                        account_id=5))
    assert pool.inserted, "два сообщения подряд схлопнулись в одну запись"


def test_the_journal_never_breaks_the_turn():
    # Журнал не имеет права сорвать ход, который он описывает.
    asyncio.run(cw._log(_Pool(raises=True), SESSION, "ok", text="привет", account_id=5))


def test_long_text_is_trimmed():
    pool = _Pool()
    asyncio.run(cw._log(pool, SESSION, "ok", text="я" * 5000, account_id=5))
    stored = [a for a in pool.inserted[0][1] if isinstance(a, str) and a.startswith("я")]
    assert stored and len(stored[0]) <= cw._LOG_TEXT_MAX


# ── Все молчаливые выходы хода ───────────────────────────────────────────────


def test_no_silent_return_is_left_in_a_turn():
    """Каждый выход из хода обязан объяснить себя.

    Проверка структурная: `return` внутри `_process_session` допустим только
    если ему предшествует запись в журнал в том же блоке. Иначе возвращается
    ровно та ситуация, с которой всё началось — флот молчит, и нигде ни слова.
    """
    node = _fn("_process_session")
    offenders: list[int] = []

    def _walk(body):
        for i, stmt in enumerate(body):
            if isinstance(stmt, ast.Return):
                before = body[:i]
                logged = any(
                    isinstance(c, ast.Call)
                    and getattr(c.func, "id", getattr(c.func, "attr", "")) in ("_log", "_skip")
                    for st in before for c in ast.walk(st)
                )
                if not logged:
                    offenders.append(stmt.lineno)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    _walk(inner)

    _walk(node.body)
    assert not offenders, (
        "в ходе разогрева остались молчаливые выходы (строки "
        + ", ".join(map(str, offenders))
        + ") — владелец снова не узнает, почему флот молчит"
    )


# Причины, которые ход обязан называть владельцу. Часть из них с появлением
# перебора кандидатов стала СВОДНОЙ: ход больше не гибнет от одного занятого
# аккаунта, поэтому «занят операцией» и «негоден» приходят не по одному, а
# строкой вида «Ни один из 6 аккаунтов не может сделать ход (занят операцией:
# 5, негоден: 1)». Формулировки проверяются в новом виде, набор причин не
# сократился.
@pytest.mark.parametrize("fragment", [
    "не выбрано ни одного аккаунта",
    "Ни один из",
    "негоден",
    "в карантине",
    "Темп снижен",
    "занят операцией",
    "Чат не даёт этому аккаунту писать",
    "Не удалось войти в чат",
    "Новых сообщений в чате нет",
    "Не задан ни один ключ ИИ",
    "Модель не вернула текст",
    "отклонён проверкой контента",
    "Telegram попросил паузу",
])
def test_every_reason_is_spelled_out_in_russian(fragment: str):
    src = _read("services", "chat_warmup.py")
    assert fragment in src, f"причина «{fragment}» нигде не называется"


# ── Что видит владелец ───────────────────────────────────────────────────────


def test_the_api_serves_the_turn_history():
    src = _read("services", "mini_app_warmup.py")
    assert 'add_get("/api/miniapp/chatwarmup/session/{sid}"' in src, (
        "открыть сессию разогрева по-прежнему нечем"
    )
    assert "chatwarmup_detail" in src
    body = src[src.index("async def chatwarmup_detail"):src.index("async def chatwarmup_accounts")]
    assert "chat_warmup_log" in body
    # Вклад по аккаунтам: владелец спрашивал именно «какие аккаунты что писали».
    assert "by_account" in body and "GROUP BY" in body
    # Подпись аккаунта, а не голый id: по id владелец аккаунт не узнаёт.
    assert "username" in body and "first_name" in body
    # Скоуп по владельцу в обоих запросах — чужой журнал не показываем.
    assert body.count("owner_id=$2") == 2


def test_the_row_in_the_list_opens():
    html = _read("mini_app", "index.html")
    m = re.search(r"async function cwLoad\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "cwLoad не найден"
    assert "cwOpenDetail(" in m.group(1), (
        "строка активного разогрева снова не открывается — ровно та жалоба, "
        "с которой всё началось"
    )
    assert 'id="s-cwdetail"' in html, "экрана хода разогрева нет"
    assert "async function cwDetailLoad()" in html


def test_the_detail_screen_shows_what_the_owner_asked_for():
    html = _read("mini_app", "index.html")
    body = html[html.index("async function cwDetailLoad()"):html.index("async function cwStatus(")]
    assert "Кто что написал" in body, "нет разбивки по аккаунтам флота"
    assert "Журнал ходов" in body, "нет журнала ходов"
    assert "Сейчас мешает" in body, (
        "экран не называет текущую причину молчания — ради неё его и открывают"
    )
    assert "esc(" in body, "текст из чата выводится без экранирования"


def test_the_migration_is_registered():
    names = [n for n in os.listdir(ROOT) if n.startswith("schema_v259")]
    assert names, "миграции журнала нет"
    sums = _read("schema_checksums.txt")
    assert names[0] in sums, (
        "миграция не внесена в schema_checksums.txt — запустите "
        "python deploy/scripts/lock_migrations.py"
    )
    sql = _read(names[0])
    assert "chat_warmup_log" in sql and "repeats" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql, "миграция не идемпотентна"


# ── Пропуск стоит столько же, сколько ход ────────────────────────────────────


def test_a_skipped_turn_waits_like_a_real_one():
    """Пропуск обязан отложить сессию, иначе цикл долбит её каждые 20 секунд.

    `last_run_at` обновлялся только в самом конце хода (`_touch`), а `_due` без
    этой отметки считает сессию созревшей всегда. Значит сессия, которая не
    может сделать ход — не вошла в чат, аккаунт занят, нет новых сообщений, —
    повторяла попытку каждые двадцать секунд круглые сутки: открывала сессию
    Telegram, вступала в чат, читала историю. Такой частоты у живого участника
    не бывает: это прямой риск бана, а не просто лишняя нагрузка.
    """
    pool = _Pool()
    asyncio.run(cw._skip(pool, SESSION, "Новых сообщений в чате нет", account_id=5))

    assert pool.inserted, "пропуск не записан в журнал"
    assert pool.updated, "сессия не отложена — следующая попытка будет через 20 секунд"
    sql, args = pool.updated[0]
    assert "chat_warmup_sessions" in sql and "last_run_at=NOW()" in sql
    assert 7 in args
    # Счётчики пропуск не трогает: это не отправленное сообщение.
    assert "messages_sent" not in sql


def test_every_early_exit_defers_the_session():
    """Все ранние выходы хода идут через _skip, а не через голый _log."""
    node = _fn("_process_session")
    bad: list[int] = []

    def _walk(body):
        for i, stmt in enumerate(body):
            if isinstance(stmt, ast.Return):
                names = [
                    getattr(c.func, "id", getattr(c.func, "attr", ""))
                    for st in body[:i] for c in ast.walk(st) if isinstance(c, ast.Call)
                ]
                if "_log" in names and "_skip" not in names:
                    bad.append(stmt.lineno)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    _walk(inner)

    _walk(node.body)
    assert not bad, (
        "ранний выход записывает причину, но не откладывает сессию (строки "
        + ", ".join(map(str, bad)) + ") — цикл будет повторять попытку каждые 20 с"
    )


def test_defer_never_breaks_the_turn():
    asyncio.run(cw._defer(_Pool(raises=True), 7))


def test_a_missing_ai_key_is_named_as_such(monkeypatch):
    """«Писать нечем» и «модель не ответила» — разные беды и разные действия."""
    import services.ai_providers as _ap
    from services import ai_claude

    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(_ap, "configured_providers", lambda: [])
    assert "ключ ИИ" in cw._no_text_reason()
    assert "Ключи ИИ" in cw._no_text_reason(), "не сказано, где задать ключ"

    monkeypatch.setattr(_ap, "configured_providers", lambda: [object()])
    assert "ключ" not in cw._no_text_reason().lower(), (
        "ключ есть, а владельцу всё равно советуют его задать"
    )


def test_an_empty_journal_still_answers_the_question():
    """Сразу после обновления журнал пуст, а вопрос «почему молчит» уже есть."""
    src = _read("services", "mini_app_warmup.py")
    body = src[src.index("async def chatwarmup_detail"):src.index("async def chatwarmup_accounts")]
    assert '"hint"' in body, "экран не отвечает ничего, пока не было ни одного хода"
    assert "configured_providers" in body and "ai_claude" in body
    assert "Ключи ИИ" in body, "подсказка не говорит, где задать ключ"
    # Подсказка только когда писать действительно нечего: при живых ходах она
    # была бы ложной тревогой.
    assert 'r["status"] == "ok"' in body

    html = _read("mini_app", "index.html")
    detail = html[html.index("async function cwDetailLoad()"):html.index("async function cwStatus(")]
    assert "r.hint" in detail, "подсказка не выводится на экране"
    assert "esc(r.hint)" in detail


def test_the_screen_answers_before_the_first_turn():
    """Экран обязан называть причину сразу, а не после следующего хода."""
    api = _read("services", "mini_app_warmup.py")
    body = api[api.index("async def chatwarmup_detail"):api.index("async def chatwarmup_accounts")]
    assert "chat_warmup.diagnose" in body and '"diagnose"' in body

    html = _read("mini_app", "index.html")
    detail = html[html.index("async function cwDetailLoad()"):html.index("async function cwStatus(")]
    assert "Почему молчит сейчас" in detail
    assert "r.diagnose" in detail
    assert "dg.ready" in detail, "не видно, сколько аккаунтов вообще готовы к ходу"
    assert "esc(a.reason)" in detail, "причина выводится без экранирования"


def test_the_diagnosis_checks_the_same_gates_as_a_turn():
    """Диагностика обязана проверять ровно то, обо что спотыкается ход."""
    src = _read("services", "chat_warmup.py")
    body = src[src.index("async def diagnose("):src.index("async def _defer(")]
    for gate in ("is_active", "session_str", "is_dead", "cooldown_until",
                 "in_operation", "is_account_quarantined", "configured_providers"):
        assert gate in body, f"диагностика не смотрит на {gate}, а ход — смотрит"
