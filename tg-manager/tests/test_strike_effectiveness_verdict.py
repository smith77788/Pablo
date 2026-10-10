"""Регрессия: Strike не удалял цели — «неверный подход» + ложное ощущение результата.

Пользователь: 100+ страйков по цели с откровенно запрещённым контентом, НИ ОДНА
цель не удалена. Причина не в баге, а в ПОДХОДЕ: движок делает ставку на массовые
in-app жалобы (account.reportPeer / messages.report) с десятков аккаунтов. Это самый
слабый вектор — Telegram не считает голоса, он взвешивает репутацию жалующегося, а
поток репортов с фарм-аккаунтов сам выглядит как координированное поведение и
обесценивается (иногда бьёт по нашим аккаунтам). К удалению публичного канала
приводят вектора юридической/платформенной ответственности: письма в abuse@/dmca@
Telegram, CSAM→NCMEC, жалоба в App Store/Google Play. Раньше сводка показывала стену
«✅» по слабым векторам → ложное ощущение результата.

Фикс (честность + маршрутизация к реальному рычагу):
- format_strike_summary добавляет честный вердикт: если цель жива и юридический
  вектор НЕ отправлен — прямо говорит, что in-app жалобы почти никогда не удаляют
  канал, и куда идти (SMTP-ящики + сторы);
- strike_status отдаёт email_vector.configured, чтобы предупредить ДО запуска;
- UI Шаг 3 показывает баннер о состоянии юридического вектора.

Тесты падают без фикса: раньше не было ни вердикта, ни флага email_vector, ни баннера.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api
from services.strike_engine import StrikeResult, _strike_effectiveness_verdict, format_strike_summary


def _index_html() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_verdict_warns_when_target_survives_and_no_legal_vector():
    r = StrikeResult(target="@bad", peer_reported=40, msgs_reported=100,
                     verified_down=False, emails_sent=0,
                     email_escalation={"skip_reason": "no email accounts configured"})
    verdict = " ".join(_strike_effectiveness_verdict(r)).lower()
    assert verdict, "должен быть честный вердикт при живой цели"
    # прямо сказано, что массовые жалобы не удаляют канал
    assert "почти никогда" in verdict and "удаля" in verdict
    # и указан реальный рычаг + куда идти
    assert "abuse@" in verdict and ("app store" in verdict or "smtp" in verdict)


def test_verdict_is_honest_when_legal_vector_did_run():
    r = StrikeResult(target="@bad", peer_reported=40, verified_down=False, emails_sent=8)
    verdict = " ".join(_strike_effectiveness_verdict(r)).lower()
    assert "письма отправлены" in verdict
    # честно: удаление — решение Telegram, гарантий нет
    assert "решение telegram" in verdict or "ни один инструмент" in verdict


def test_no_verdict_when_target_confirmed_down():
    r = StrikeResult(target="@bad", peer_reported=40, verified_down=True, emails_sent=0)
    assert _strike_effectiveness_verdict(r) == [], (
        "цель подтверждённо удалена — вердикт-подсказка не нужен"
    )


def test_summary_embeds_verdict_for_surviving_target():
    r = StrikeResult(target="@bad", peer_reported=40, verified_down=False, emails_sent=0,
                     email_escalation={"skip_reason": "no email accounts configured"})
    summary = format_strike_summary([r])
    assert "Итог по цели" in summary, "сводка должна нести честный вердикт по цели"


def test_strike_status_exposes_email_vector():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def strike_status\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "strike_status не найден"
    body = m.group(1)
    assert "strike_email_accounts" in body and '"email_vector"' in body, (
        "strike_status должен отдавать состояние юридического (email) вектора"
    )


def test_ui_has_vector_warning_banner():
    html = _index_html()
    assert 'id="strikeVectorWarn"' in html, "нет баннера состояния юридического вектора"
    # баннер должен заполняться из email_vector статуса
    assert "email_vector" in html, "UI должен читать email_vector из статуса Strike"
