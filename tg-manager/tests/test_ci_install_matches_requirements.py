"""CI ставит не requirements.txt целиком, а курируемое подмножество — и оно тихо
отстаёт от кода: пакет добавили в requirements.txt и стали безусловно
импортировать в проде, а в подмножество для CI забыли продублировать.

Живой случай (эта сессия): CI почти двое суток не выделял раннер вообще (это
отдельная, не связанная с кодом проблема биллинга GitHub Actions), и когда
раннер вернулся — 7 тестов QR-логина (tests/test_qr_login_no_leaked_client.py)
упали `ModuleNotFoundError: No module named 'qrcode'`. Пакет есть в
requirements.txt (`qrcode[pil]>=7.4`, используется services/account_manager.py
`start_qr_login`), но комментарий в самом workflow уже честно предупреждал: то
же самое раньше случилось с `anthropic` («он уже в requirements.txt — здесь
просто не был продублирован»). Один раз это исправили руками, но не оставили
проверку — и класс бага повторился.

Локально это не ловится: в песочнице агента `qrcode` (и весь requirements.txt)
уже стоит, `pytest tests/ -q` зелёный. Расхождение видно только сравнением с
тем, что реально ставит workflow-файл — отсюда этот тест.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# Пакеты, для которых прод-код делает БЕЗУСЛОВНЫЙ `import X` на уровне модуля
# (не в try/except) и которые НЕ застаблены в tests/conftest.py (как telethon).
# Без них падает уже СБОР тестов, а не отдельный тест — то есть эффект
# максимально скрытный. Пополняется только по факту нового найденного случая,
# не заранее.
REQUIRED_CI_PACKAGES = {
    "anthropic",  # tests/test_ai_claude.py: import anthropic
    "qrcode",     # services/account_manager.py start_qr_login: import qrcode
}


def _install_step_text() -> str:
    src = WORKFLOW.read_text(encoding="utf-8")
    i = src.index("Install deps")
    j = src.index("\n      - name:", i)
    return src[i:j]


def _installed_packages(step_text: str) -> set[str]:
    # Из `pip install \` блока вытаскиваем токены пакетов: снимаем кавычки,
    # версийные пины (==/>=) и экстры ([pil]).
    m = re.search(r"pip install \\(.*?)(?=\n\s*\n|\Z)", step_text, re.DOTALL)
    assert m, "не нашли `pip install \\` блок — формат workflow изменился"
    raw = m.group(1)
    tokens = re.findall(r'"?([A-Za-z0-9_.\-]+)(?:\[[a-z]+\])?(?:[=<>!~]{1,2}[\w.]+)?"?', raw)
    return {t.lower() for t in tokens if t}


def test_ci_install_step_has_known_fragile_packages():
    step = _install_step_text()
    installed = _installed_packages(step)
    missing = {p for p in REQUIRED_CI_PACKAGES if p.lower() not in installed}
    assert not missing, (
        f"CI не ставит пакеты {missing}, хотя прод-код их безусловно "
        "импортирует (см. requirements.txt) — тесты будут падать "
        "ModuleNotFoundError сразу на сборе, локально в песочнице агента это "
        "не видно (пакет там уже стоит). Добавьте в `pip install` шаг "
        "Install deps в .github/workflows/tests.yml."
    )
