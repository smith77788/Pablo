"""Указатель на сообщение с прогресс-баром не попадает в личность операции.

ЧТО ЭТО ЗА КЛАСС ОШИБКИ. Окно идемпотентности `operation_bus.submit` считает
два сабмита одним, если совпадают owner, op_type, scheduled_for и params.
Значит любое значение в params, которое меняется от тапа к тапу, выключает
дедуп НАСОВСЕМ — и выключает его молча: защита на месте, тест на двойной тап
зелёный, а двойной тап проходит насквозь.

Именно так и было: два места кладут в params указатель на сообщение владельца
с прогресс-баром, а сообщение создаётся ДО постановки, то есть у каждого тапа
своё. `bulk_post_to_channel` публиковал от всех аккаунтов дважды.

ЧТО ЗАКРЕПЛЕНО ЗДЕСЬ.
1. Косметические ключи вычёркиваются из идентичности (и из отпечатка), а не
   сравниваются вместе с остальным params.
2. Сравнение «целого params» в запрос дедупа не возвращается.
3. Ни один вызов submit() не кладёт id свежесозданного сообщения в params под
   именем, которое из идентичности не вычёркивается.

ПОЧЕМУ ВЫЧЁРКИВАЮТСЯ ИМЕННО `_progress_*`, А НЕ `chat_id`/`message_id`. Ключ
`chat_id` у многих операций — ЦЕЛЬ, а не косметика. Вычеркнув его, мы получили
бы болезнь хуже: два сабмита на разных адресатов слиплись бы в одну операцию,
и вторая тихо не выполнилась бы вовсе. Префикс `_progress_` цели обозначать не
может по построению.
"""

import ast
import pathlib
import re

from services import operation_bus

ROOT = pathlib.Path(operation_bus.__file__).resolve().parent.parent
BUS = pathlib.Path(operation_bus.__file__)

# Значение, которое выдаёт указатель на сообщение: id объекта сообщения,
# полученного рядом (`msg.message_id`, `sent.message_id`, …).
POINTER_VALUE = re.compile(r"\.message_id\b|\bmessage_id=\b")


def _submit_params_sources(src: str) -> list[tuple[int, str]]:
    """(строка, исходник литерального params) для каждого вызова submit()."""
    out: list[tuple[int, str]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        if name != "submit":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        params = kw.get("params")
        if params is None and len(node.args) >= 4:
            params = node.args[3]
        if not isinstance(params, ast.Dict):
            continue          # params собран выше — литерала здесь нет
        for key, value in zip(params.keys, params.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            vsrc = ast.get_source_segment(src, value) or ""
            out.append((getattr(value, "lineno", 0), f"{key.value}={vsrc}"))
    return out


def _pointer_findings(src: str) -> list[str]:
    """Ключи params, в которые кладут id сообщения, но не вычёркивают."""
    bad = []
    for lineno, pair in _submit_params_sources(src):
        key, _, vsrc = pair.partition("=")
        if key in operation_bus.DEDUP_COSMETIC_KEYS:
            continue
        if POINTER_VALUE.search(vsrc):
            bad.append(f"строка {lineno}: {key} = {vsrc}")
    return bad


def test_no_call_site_puts_a_message_pointer_into_the_identity():
    findings = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "/.venv/" in rel:
            continue
        for item in _pointer_findings(path.read_text(encoding="utf-8")):
            findings.append(f"{rel} {item}")
    assert not findings, (
        "в params операции кладут id свежесозданного сообщения под ключом, "
        "который не вычёркивается из дедуп-идентичности — у каждого тапа он "
        "свой, и дедуп не сработает НИ РАЗУ:\n  " + "\n  ".join(findings)
        + f"\nКосметические ключи: {operation_bus.DEDUP_COSMETIC_KEYS}"
    )


def test_the_measurer_sees_the_difference():
    """Измеритель проверен на заведомо больном и заведомо здоровом примере."""
    sick = (
        "async def h(pool, uid, msg):\n"
        "    await operation_bus.submit(pool, uid, 'x', {\n"
        "        'account_ids': [1], 'message_id': msg.message_id})\n"
    )
    healthy = (
        "async def h(pool, uid, msg):\n"
        "    await operation_bus.submit(pool, uid, 'x', {\n"
        "        'account_ids': [1], '_progress_message_id': msg.message_id})\n"
    )
    assert _pointer_findings(sick), "измеритель не видит больной пример"
    assert not _pointer_findings(healthy), "измеритель ругается на здоровый"


def test_the_cosmetic_keys_are_struck_out_of_the_identity():
    src = {"account_ids": [1, 2], "_progress_chat_id": 7,
           "_progress_message_id": 9}
    assert operation_bus._dedup_identity_params(src) == {"account_ids": [1, 2]}
    assert operation_bus.DEDUP_COSMETIC_KEYS, "список косметических ключей пуст"
    assert all(k.startswith("_") for k in operation_bus.DEDUP_COSMETIC_KEYS), (
        "косметическим объявлен ключ без служебного префикса — такой может "
        "оказаться целью операции, и его вычёркивание склеит разные операции"
    )


def _submit_src() -> str:
    tree = ast.parse(BUS.read_text(encoding="utf-8"))
    src = BUS.read_text(encoding="utf-8")
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "submit":
            return ast.get_source_segment(src, fn) or ""
    raise AssertionError("submit не найдена в operation_bus — проверку перечитать")


def test_the_query_does_not_compare_whole_params_again():
    body = _submit_src()
    assert "params - $6::text[] = $3::jsonb" in body, (
        "запрос дедупа больше не вычёркивает косметические ключи — проверьте, "
        "не вернулось ли сравнение целого params"
    )
    assert not re.search(r"AND params = \$3::jsonb", body), (
        "в запрос дедупа вернулось сравнение целого params: указатель на "
        "прогресс-бар снова ломает идентичность, и двойной тап снова даст "
        "две массовые операции"
    )


def test_the_fingerprint_is_built_without_the_cosmetic_keys():
    """Отпечаток чувствительных params — тоже идентичность, и тоже без косметики.

    Проверка статическая намеренно: в отпечаток идут только params с секретами
    (пароли, токены), и подставлять их в живой тест ради этой проверки значило
    бы держать секретоподобные данные в тестовых фикстурах.
    """
    body = _submit_src()
    assert "seal_operation_params(\n        _dedup_identity_params(params))" in body, (
        "отпечаток считается от целого params — указатель на прогресс-бар "
        "будет менять его на каждом тапе, и дедуп по отпечатку ослепнет"
    )
