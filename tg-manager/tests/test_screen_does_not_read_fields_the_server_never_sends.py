"""Поле, которое экран читает в ответе, сервер обязан присылать.

КЛАСС ОШИБОК — обратный к `tests/test_screen_fields_are_not_dropped.py`: там
поле ЗАПРОСА, которое обработчик не читает, здесь поле ОТВЕТА, которого
обработчик не присылает. Для владельца это худший вид поломки: ничего не
падает. Экран рисует пустую колонку, пустое число, «undefined» или просто
молчит — и узнать, что цифры нет, а не что она ноль, неоткуда.

Класс ловился в проекте руками:

  * Выгрузка операций. Кнопка «Экспорт CSV» собирала файл из разметки
    экрана. Три колонки из шести — ID, «Создана» и «Прогресс» — писались
    пустыми ВСЕГДА, потому что этих полей в разметке нет вовсе. Файл при
    этом скачивался, с правильным заголовком и строками.
  * Деплой пула Cloudflare. Ветка отказа печатала число назначенных
    аккаунтов из поля, которого в ответе нет: владелец на отказе читал
    бодрое «назначено: 0».
  * Синхронизация контактов. Ветка «не встало в очередь» показывала
    `message` из ответа — такого поля ручка не присылает, условие было
    ложным всегда, и после «🔄 Запускаю…» владелец не получал НИ слова.

ЧТО СВЕРЯЕТСЯ. Каждый вызов вида `const d = await api('/api/miniapp/...')`
во фронте: путь И МЕТОД сопоставляются с зарегистрированным маршрутом, у
обработчика берутся ключи ответа, и каждое чтение `d.<поле>` обязано быть
среди них.

ГРАНИЦЫ, без которых детектор врёт (все три проверены на этом коде):

  * Метод обязателен. На одном пути часто висят GET и POST (`/autoreg` —
    сводка и постановка). Без метода поля ответа POST сверялись с ответом
    GET: девять ложных находок из четырнадцати.
  * Переменная читается только до следующего присваивания ей же. В одной
    функции часто несколько `api` подряд в одну и ту же `d`.
  * Строчные комментарии вырезаются. Пояснение к правке называет само поле,
    из-за которого правка делалась, — на этом уже попадался храповик
    выгрузки операций.

Сверяются ТОЛЬКО ручки, чей ответ собран литералом: `_json_resp(x)`,
`{**что-то}` и словарь из `dict(row)` набор ключей не выдают, и требовать от
них состав ответа значит выдумывать. Это не исключения по списку, а граница
применимости: про такие ручки детектор молчит, а не угадывает.

ИЗМЕРЕНИЕ на момент написания: 199 пар экран↔ручка с литеральным ответом,
находок — ноль.
"""
from __future__ import annotations

import ast
import pathlib
import re

from tests.miniapp_source import api_function, api_routes, miniapp_source

SERVICES = pathlib.Path(__file__).resolve().parent.parent / "services"


def _code_only(src: str) -> str:
    """Код без строчных комментариев."""
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in src.splitlines())


UI = _code_only(miniapp_source())
ROUTES = api_routes()


def _js_functions(src: str):
    """(имя, тело) каждой JS-функции — границы по балансу фигурных скобок."""
    for m in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\(", src):
        i = src.index("{", m.end() - 1)
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    yield m.group(1), src[i:j + 1]
                    break


def _handler_of(path: str, method: str) -> str | None:
    """Имя обработчика по пути И методу — из строки регистрации в слое."""
    for p in sorted(SERVICES.glob("mini_app*.py")):
        m = re.search(r'add_' + method.lower() + r'\(\s*"' + re.escape(path)
                      + r'"\s*,\s*(\w+)', p.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return None


def _response_keys(src: str) -> tuple[set[str], bool]:
    """Ключи ответа, если он собран ЛИТЕРАЛОМ; второе — «ответ динамический»."""
    try:
        tree = ast.parse(src.lstrip())
    except SyntaxError:
        return set(), True
    keys: set[str] = set()
    dynamic = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_json_resp"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                dynamic = True
                continue
            for k in arg.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
                else:
                    dynamic = True
    return keys, dynamic


def _pairs():
    """(функция экрана, путь, метод, обработчик, читаемые поля, поля ответа)."""
    for fname, body in _js_functions(UI):
        for m in re.finditer(
                r"(?:const|let|var)\s+(\w+)\s*=\s*await\s+api\(\s*'(/api/miniapp/[^'?]+)'",
                body):
            var, path = m.group(1), m.group(2)
            if path not in ROUTES:
                continue
            opts = body[m.end():m.end() + 400].split(")")[0]
            mm = re.search(r"method\s*:\s*'(\w+)'", opts)
            method = (mm.group(1) if mm else "GET").upper()
            handler = _handler_of(path, method)
            if not handler:
                continue
            try:
                hsrc = api_function(handler)
            except AssertionError:
                continue
            keys, dynamic = _response_keys(hsrc)
            if dynamic or not keys:
                continue
            after = body[m.end():]
            nxt = re.search(r"(?:const|let|var)\s+" + re.escape(var) + r"\s*=|\b"
                            + re.escape(var) + r"\s*=\s*await\s+api\(", after)
            scope = after[:nxt.start()] if nxt else after
            reads = set(re.findall(r"\b" + re.escape(var) + r"\.(\w+)\b", scope))
            yield fname, path, method, handler, reads, keys


_PAIRS = list(_pairs())


def test_the_probe_sees_the_screens_at_all():
    """Детектор, который ничего не разобрал, зелёный всегда."""
    assert len(UI) > 500_000, f"фронт прочитан не весь: {len(UI)} байт"
    assert len(ROUTES) > 600, f"маршрутов разобрано {len(ROUTES)}"
    assert len(_PAIRS) >= 150, (
        f"пар экран↔ручка разобрано {len(_PAIRS)}, было 199 — детектор "
        f"перестал находить вызовы, а не код стал чище")
    # 384 чтения на 199 парах — измерено 16.10.2026. Порог ниже замера:
    # он сторожит «детектор перестал читать поля», а не точное число.
    assert sum(len(r) for *_, r, _ in _PAIRS) >= 300, "поля ответа не читаются"


def test_no_screen_reads_a_field_the_handler_never_sends():
    offenders = []
    for fname, path, method, handler, reads, keys in _PAIRS:
        missing = sorted(reads - keys)
        if missing:
            offenders.append(
                f"  {fname}() → {method} {path} [{handler}]: читает "
                f"{missing}, а ответ отдаёт {sorted(keys)}")
    assert not offenders, (
        "экран читает поля, которых сервер не присылает — он покажет пустоту "
        "или «undefined», и ничего при этом не упадёт:\n" + "\n".join(offenders))
