"""Сетевая безопасность: на БОЕВЫХ эндпоинтах TLS-верификация не отключается.

Аудит (Стадия 2, пункт #1): `ssl=False` / `CERT_NONE` / `check_hostname=False` на
доверенных внешних API (LLM-провайдеры, SMM-панель, воронка) — это MITM-вектор:
ключ и данные можно перехватить/подменить. Отключение проверки допустимо ТОЛЬКО на
пути тестирования заведомо недоверенных публичных прокси, и там оно помечено
явным комментарием «намеренно».

Тест падает без фикса (при возврате ssl=False на боевой путь) и проходит с ним.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Модули, где идут вызовы к ДОВЕРЕННЫМ внешним API — TLS обязателен.
REAL_API_FILES = [
    "services/ai_providers.py",
    "services/smm_panel.py",
    "services/auto_funnel.py",
    "bot/handlers/admin.py",
]

_TLS_OFF = re.compile(r"ssl\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False")


@pytest.mark.parametrize("rel", REAL_API_FILES)
def test_no_tls_disable_on_real_endpoints(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    offenders = [
        (i + 1, ln.strip())
        for i, ln in enumerate(src.splitlines())
        if _TLS_OFF.search(ln)
    ]
    assert not offenders, (
        f"{rel}: TLS-верификация отключена на боевом эндпоинте (MITM-риск): {offenders}"
    )


def test_proxy_test_paths_marked_intentional():
    """Там, где ssl=False оставлен (тест недоверенных прокси), он должен быть
    ЯВНО помечен — чтобы отключение проверки не расползлось молча на боевые пути."""
    for rel in ("services/proxy_selector.py", "services/proxy_scraper.py",
                "bot/handlers/proxy_manager.py"):
        for i, ln in enumerate(( ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if "ssl=False" in ln:
                assert "намеренно" in ln, (
                    f"{rel}:{i} — ssl=False без пометки «намеренно»: подтвердите, что это "
                    "путь проверки недоверенного прокси, а не боевой вызов"
                )
