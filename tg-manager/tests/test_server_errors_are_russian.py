"""Человеку показывали текст исключения — по-английски.

Отказ сервера в мини-аппе выглядел так:

    except Exception as exc:
        return _err(str(exc), 500)

и человек видел на экране `'NoneType' object has no attribute 'id'` или
`relation "vault_messages" does not exist`. Владелец продукта английского не
читает вовсе — для него это пустая строка символов; но и тому, кто читает,
делать с ней нечего: это внутреннее устройство, а не то, что можно исправить
своими действиями.

Таких мест было 398, плюс три десятка английских литералов вроде
`Failed to create broadcast` и `db error`. Восемьдесят два из них вообще не
оставляли следа в логах: отказ уходил человеку и нигде не сохранялся, так что
узнать о поломке было нечем.

Теперь наружу уходит русская фраза, а исключение целиком — в лог. Этот
храповик держит оба условия: всякое сообщение с кодом 5xx — русское, и всякий
обработчик, который отвечает 5xx, пишет в лог.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "services"
CYRILLIC = re.compile("[а-яёА-ЯЁ]")


def _modules() -> list[Path]:
    return sorted(SERVICES.glob("mini_app*.py"))


def _err_calls(tree: ast.AST):
    """Вызовы `_err(...)`, у которых код состояния — 5xx."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_err"):
            continue
        status = node.args[1] if len(node.args) > 1 else None
        code = status.value if isinstance(status, ast.Constant) else None
        if isinstance(code, int) and code >= 500:
            yield node


def test_the_probe_sees_the_refusals():
    """Страховка измерителя: не найдя вызовов, тест молча «проходит» всегда."""
    total = sum(len(list(_err_calls(ast.parse(p.read_text("utf-8")))))
                for p in _modules())
    assert total > 300, (
        f"найдено всего {total} отказов 5xx — проверка измеряет не то")


def test_every_server_refusal_speaks_russian():
    bad = []
    for p in _modules():
        src = p.read_text("utf-8")
        for node in _err_calls(ast.parse(src)):
            msg = node.args[0]
            # Общая фраза объявлена один раз в mini_app_api; её текст
            # проверяется отдельным тестом ниже.
            if isinstance(msg, ast.Name) and msg.id == "_INTERNAL_ERROR":
                continue
            if isinstance(msg, ast.Constant) and isinstance(msg.value, str) \
                    and CYRILLIC.search(msg.value):
                continue
            shown = ast.dump(msg)[:80]
            bad.append(f"{p.name}:{node.lineno} {shown}")
    assert not bad, (
        "отказ сервера уходит человеку не русской фразой, а вычисленным "
        "текстом (чаще всего — текстом исключения):\n" + "\n".join(bad[:20]))


def test_the_shared_phrase_is_russian_and_says_what_to_do():
    src = (SERVICES / "mini_app_api.py").read_text("utf-8")
    m = re.search(r'^_INTERNAL_ERROR = "([^"]+)"', src, re.M)
    assert m, "общая фраза отказа пропала — её место займёт текст исключения"
    phrase = m.group(1)
    assert CYRILLIC.search(phrase), "общая фраза отказа не на русском"
    assert not re.search(r"[A-Za-z]{4}", phrase), (
        f"в общей фразе отказа английские слова: {phrase!r}")


def test_a_server_refusal_leaves_a_trace_in_the_log():
    """Отказ, о котором не узнал никто, — поломка, которой как бы и нет."""
    silent = []
    for p in _modules():
        src = p.read_text("utf-8")
        lines = src.split("\n")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not any(_err_calls(node)):
                continue
            has_log = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name) and c.func.value.id == "log"
                for c in ast.walk(node))
            if not has_log:
                head = lines[node.lineno - 1].strip()
                silent.append(f"{p.name}:{node.lineno} {head}")
    assert not silent, (
        "обработчик отвечает 500 и ничего не пишет в лог — о поломке не "
        "узнает никто:\n" + "\n".join(silent[:20]))

# ── Отказы 4xx ───────────────────────────────────────────────────────────────

def test_literal_refusals_speak_russian_at_any_status():
    """4xx тут уже в порядке — тест держит это состояние.

    Из 1666 литеральных сообщений с кодом 4xx по-английски было два:
    `Unauthorized` (619 раз) и `Invalid Telegram initData`. Оба с кодом 401, и
    до человека их текст не доходит — мини-апп на 401 показывает свою фразу
    «Сессия истекла, перезапустите приложение». Остальное — русское, и пусть
    таким остаётся.

    Вычисленные сообщения (`_err(str(ve), 400)`) намеренно не проверяются: в
    `services/account_manager.py` ValueError поднимают русским текстом именно
    для показа человеку («Неверный код — проверьте и введите снова»). Запрет
    пробросить его обратил бы осмысленную подсказку в общую фразу.
    """
    allowed = {"Unauthorized", "Invalid Telegram initData"}
    bad = []
    for p in _modules():
        for node in ast.walk(ast.parse(p.read_text("utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_err"):
                continue
            status = node.args[1] if len(node.args) > 1 else ast.Constant(400)
            code = status.value if isinstance(status, ast.Constant) else None
            if not isinstance(code, int) or code >= 500:
                continue
            msg = node.args[0]
            if not (isinstance(msg, ast.Constant) and isinstance(msg.value, str)):
                continue
            v = msg.value
            if v in allowed or CYRILLIC.search(v) or not re.search("[A-Za-z]{3}", v):
                continue
            bad.append(f"{p.name}:{node.lineno} {v!r}")
    assert not bad, (
        "человеку показывают английский отказ:\n" + "\n".join(bad[:20]))
