"""Метод «ссылка в ЛС»: свой текст и разный текст на каждую цель.

ЧТО БЫЛО СЛОМАНО. Шаблон разворачивался ОДИН РАЗ до цикла, поэтому весь флот слал
побайтово одинаковое сообщение сотням людей — самая узнаваемая подпись спама,
какая бывает, и именно в методе, который продукт называет «безопаснее всего».
Движок spintax в проекте есть и используется рассылкой; здесь он не вызывался.

Плюс пауза стояла ТОЛЬКО на успешной ветке: серия отказов (приватность ЛС
отвечает быстро) прогоняла цикл вообще без задержек — ровно тот всплеск частоты,
из-за которого прилетает PeerFlood.

И в боте не было поля для текста: метод был доступен, но всегда с одной и той же
фразой по умолчанию.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import mass_inviter_engine as mie

ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _stand(monkeypatch, results=None):
    sent: list[tuple[str, str]] = []

    async def _send_dm(session, ref, text, _acc=None):
        sent.append((str(ref), text))
        return (results or {}).get(str(ref), {"ok": True})

    import services.account_manager as am
    monkeypatch.setattr(am, "send_dm", _send_dm)

    slept: list[float] = []

    async def _sleep(sec):
        slept.append(float(sec))

    monkeypatch.setattr(mie.asyncio, "sleep", _sleep)
    return sent, slept


def test_each_target_gets_its_own_spintax_variant(monkeypatch):
    sent, _ = _stand(monkeypatch)
    refs = [f"@u{i}" for i in range(40)]
    _run(mie.invite_via_link_batch(
        "s", {"id": 1}, "https://t.me/+ABC", refs,
        "{Привет|Здравствуйте|Добрый день}! Заходи: {link}"))
    texts = {t for _, t in sent}
    assert len(texts) > 1, (
        "весь флот отправил побайтово одинаковый текст — это подпись спама"
    )
    assert all("https://t.me/+ABC" in t for _, t in sent), "ссылка потерялась"


def test_link_placeholder_survives_spintax(monkeypatch):
    """Для лексера `{link}` — группа из одного варианта, и он раскрывает её в
    слово «link», съедая место под ссылку."""
    sent, _ = _stand(monkeypatch)
    _run(mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+XYZ", ["@a"],
                                   "Вот ссылка: {link} — заходи"))
    assert "https://t.me/+XYZ" in sent[0][1]
    assert "link —" not in sent[0][1], "плейсхолдер был раскрыт как spintax-группа"


def test_link_appended_when_placeholder_missing(monkeypatch):
    sent, _ = _stand(monkeypatch)
    _run(mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+Q", ["@a"],
                                   "Текст без плейсхолдера"))
    assert sent[0][1].rstrip().endswith("https://t.me/+Q")


def test_pause_happens_after_failures_too(monkeypatch):
    """Серия отказов не должна прогонять цикл без задержек."""
    refs = [f"@u{i}" for i in range(5)]
    sent, slept = _stand(
        monkeypatch,
        results={r: {"ok": False, "error": "privacy restricted"} for r in refs})
    res = _run(mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+A", refs))
    assert res["failed"] == 5
    assert len(slept) >= 5, f"после отказов пауз не было: {slept}"


def test_pace_multiplier_scales_pauses(monkeypatch):
    refs = ["@a", "@b"]
    _, slow = _stand(monkeypatch)
    _run(mie.invite_via_link_batch("s", {"id": 1}, "l", refs, None, 2.5))
    _, fast = _stand(monkeypatch)
    _run(mie.invite_via_link_batch("s", {"id": 1}, "l", refs, None, 0.5))
    assert min(slow) > max(fast), "режим темпа не влияет на паузы метода «ссылка»"


def test_failures_are_classified_not_dumped_as_other(monkeypatch):
    refs = ["@a"]
    _stand(monkeypatch, results={"@a": {"ok": False, "error": "privacy restricted"}})
    res = _run(mie.invite_via_link_batch("s", {"id": 1}, "l", refs))
    assert res["fail_kinds"].get(mie.FAIL_PRIVACY) == 1


# ── Ввод текста в боте ───────────────────────────────────────────────────────

BOT = (ROOT / "bot" / "handlers" / "mass_inviter.py").read_text(encoding="utf-8")


def test_bot_asks_for_link_text():
    assert "link_message = State()" in BOT, "нет состояния для ввода текста"
    assert re.search(r'if method == "link":\s*\n(\s*#.*\n)*\s*await state\.set_state\('
                     r'InviterFSM\.link_message\)', BOT), (
        "после выбора метода «ссылка» бот обязан спросить текст"
    )


def test_bot_offers_default_and_passes_text_to_operation():
    assert 'InviterCb(action="link_default")' in BOT, "нужен выход «взять стандартный»"
    assert 'params["link_message"]' in BOT, "введённый текст не доезжает до операции"


def test_bot_validates_spintax_before_launch():
    """Незакрытая скобка означала бы, что каждому уходит сырая разметка —
    увидели бы это только получатели."""
    i = BOT.index("async def msg_inviter_link_message")
    seg = BOT[i:i + 1800]
    assert "expand_spintax" in seg and "скобк" in seg
    assert "1000" in seg, "длина текста должна быть ограничена"
