"""Восемь тестов «мини-апп встаёт, когда Telegram недоступен» не должны снова
стать невыполняемыми.

РАЗРЫВ. tests/test_startup_survives_telegram_outage.py поднимает настоящий
`main.py` ОТДЕЛЬНЫМ ПРОЦЕССОМ: только так видно, что продукт стартует, когда
Telegram недоступен (а он лежал целиком, и ни один из трёх с половиной тысяч
тестов этого не заметил). Заглушка telethon из tests/conftest.py живёт внутри
процесса pytest и в подпроцесс не попадает, поэтому без настоящего пакета все
восемь честно, но молча уходят в skip — и до 2026-10-22 они не выполнялись
НИГДЕ: ни локально, ни в прогоне ветки. Тест, который не выполняется, не
охраняет ничего.

Пакет теперь ставит отдельный шаг workflow. Этот тест охраняет сам шаг: уберут
его (или уберут `continue-on-error`, и тогда упавшая сборка колеса покрасит
весь прогон) — красным станет здесь, а не тишиной на восьми скипах.

ПОЧЕМУ ОТДЕЛЬНЫМ ШАГОМ, А НЕ В ОБЩИЙ СПИСОК. `pip` при одном упавшем пакете не
ставит НИ ОДНОГО из списка: прогон потом падает на `ModuleNotFoundError:
pytest`, и на разбор пустой поломки уходит время.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# Шаги workflow: имя → его текст целиком (до следующего `- name:`).
_STEP_RE = re.compile(r"\n      - name: (?P<name>.*?)\n(?P<body>.*?)(?=\n      - name: |\Z)",
                      re.DOTALL)


def _steps(src: str) -> list[tuple[str, str]]:
    return [(m.group("name"), m.group("body")) for m in _STEP_RE.finditer(src)]


def _without_comments(body: str) -> str:
    """Комментарии не ставят пакетов. Шаг Install deps РАССКАЗЫВАЕТ про telethon
    («застаблен в conftest») — по тексту комментария его легко принять за
    установку, и измеритель соврёт в обе стороны сразу."""
    return "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))


def _telethon_steps(src: str) -> list[tuple[str, str]]:
    out = []
    for name, body in _steps(src):
        code = _without_comments(body)
        if "pip install" in code and re.search(r"\btelethon\b", code):
            out.append((name, body))
    return out


def _problems(src: str) -> list[str]:
    """Что не так с установкой telethon в этом тексте workflow."""
    found = _telethon_steps(src)
    if not found:
        return ["ни один шаг не ставит telethon"]
    bad = []
    for name, body in found:
        if "Install deps" in name:
            bad.append(f"telethon попал в общий список пакетов (шаг «{name}»)")
        elif "continue-on-error: true" not in body:
            bad.append(f"у шага «{name}» нет continue-on-error: true")
    return bad


def test_ci_installs_telethon_in_its_own_forgiving_step():
    problems = _problems(WORKFLOW.read_text(encoding="utf-8"))
    assert not problems, (
        "; ".join(problems) + ". Без настоящего telethon восемь тестов "
        "tests/test_startup_survives_telegram_outage.py молча уходят в skip, и "
        "продукт может снова не стартовать так, что прогон ветки этого не "
        "увидит. Шаг должен быть ОТДЕЛЬНЫМ (pip при одном упавшем пакете не "
        "ставит ни одного из списка) и с continue-on-error: true (не собралось "
        "колесо — прогон идёт дальше, эти восемь снова в skip)."
    )


# ── Самопроверка измерителя: он обязан находить то, что ищет ─────────────────

_SAMPLE = """
jobs:
  tests:
    steps:
      - name: Install deps
        run: |
          pip install \\
            pytest ruff
      - name: Install telethon
        continue-on-error: true
        run: pip install "telethon==1.45.0" pyaes rsa
      - name: Run tests
        run: pytest -q
"""


def test_the_detector_sees_a_healthy_workflow_as_healthy():
    assert _problems(_SAMPLE) == []


def test_the_detector_notices_the_step_removed():
    broken = _SAMPLE.replace(
        '      - name: Install telethon\n'
        '        continue-on-error: true\n'
        '        run: pip install "telethon==1.45.0" pyaes rsa\n', "")
    assert _problems(broken) == ["ни один шаг не ставит telethon"]


def test_the_detector_notices_the_forgiveness_removed():
    broken = _SAMPLE.replace("        continue-on-error: true\n", "")
    assert _problems(broken) == ["у шага «Install telethon» нет continue-on-error: true"]


def test_the_detector_notices_telethon_moved_into_the_common_list():
    broken = _SAMPLE.replace("            pytest ruff", '            pytest ruff telethon')
    broken = broken.replace(
        '      - name: Install telethon\n'
        '        continue-on-error: true\n'
        '        run: pip install "telethon==1.45.0" pyaes rsa\n', "")
    assert _problems(broken) == ["telethon попал в общий список пакетов (шаг «Install deps»)"]


def test_the_detector_does_not_count_a_comment_as_an_install():
    chatty = _SAMPLE.replace(
        "        run: |\n          pip install \\",
        "        run: |\n          # telethon НЕ ставим — он застаблен в conftest\n"
        "          pip install \\")
    assert _problems(chatty) == []
