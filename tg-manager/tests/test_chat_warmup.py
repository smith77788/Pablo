"""Разогрев чата флотом с осмысленным диалогом — ЧИСТОЕ ядро.

Проверяем логику «кто говорит и на что отвечает» (plan_turn), сборку промпта с
анти-бот правилами (build_dialogue_prompt) и очистку ответа LLM (sanitize_reply).
Сеть/LLM/БД здесь не участвуют.
"""
from __future__ import annotations

import os

from services import chat_warmup as cw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _msg(i, text, fleet=False, name="Гость"):
    return {"id": i, "sender_id": 100 + i, "sender_name": name,
            "text": text, "is_fleet": fleet}


def test_modes_and_intensity():
    assert cw.valid_mode("engage") == "engage"
    assert cw.valid_mode("bogus") == "mixed"
    assert cw.valid_mode(None) == "mixed"
    lo, hi = cw.intensity_delay("active")
    assert 0 < lo < hi
    assert cw.intensity_delay("xxx") == cw.INTENSITY["normal"]


def test_engage_needs_external_message():
    recent = [_msg(1, "привет всем", fleet=True)]   # только флот
    assert cw.plan_turn(recent, [1, 2], "engage") is None   # некому отвечать → пропуск


def test_engage_replies_to_newest_external():
    recent = [_msg(1, "флот", fleet=True), _msg(2, "а кто пойдёт в кино?", name="Аня")]
    p = cw.plan_turn(recent, [10, 11], "engage", last_speaker=None)
    assert p and p["kind"] == "reply"
    assert p["reply_to_id"] == 2 and "кино" in p["reply_to_text"]
    assert p["reply_to_sender"] == "Аня"
    assert p["speaker"] in (10, 11)


def test_seed_ignores_external_and_talks():
    recent = [_msg(2, "внешнее сообщение", name="Гость")]
    p = cw.plan_turn(recent, [10, 11], "seed")
    assert p and p["kind"] == "seed" and p["reply_to_id"] is None


def test_mixed_prefers_external_else_seed():
    with_ext = cw.plan_turn([_msg(3, "вопрос", name="Гость")], [10], "mixed")
    assert with_ext["kind"] == "reply"
    only_fleet = cw.plan_turn([_msg(3, "флот болтает", fleet=True)], [10], "mixed")
    assert only_fleet["kind"] == "seed"


def test_last_seen_filters_processed_external():
    recent = [_msg(5, "старое, уже ответили", name="Гость")]
    # last_seen_msg=5 → сообщение 5 уже обработано → в engage отвечать не на что
    assert cw.plan_turn(recent, [10], "engage", last_seen_msg=5) is None


def test_speaker_avoids_repeat_and_empty_fleet():
    p = cw.plan_turn([_msg(1, "x", fleet=True)], [10, 11], "seed", last_speaker=10)
    assert p["speaker"] == 11               # не повторяем последнего говорящего
    assert cw.plan_turn([], [], "seed") is None   # пустой флот → нет хода


def test_prompt_has_antibot_rules_and_context():
    recent = [_msg(1, "как дела", name="Макс")]
    system, user = cw.build_dialogue_prompt("Весёлый геймер", recent,
                                            {"text": "как дела", "sender_name": "Макс"},
                                            "игры, кино", "engage")
    assert "Весёлый геймер" in system
    assert "не раскрывай" in system.lower() and "русск" in system.lower()
    assert "Макс" in user and "как дела" in user      # контекст + цель ответа
    assert user.strip()


def test_prompt_seed_uses_topics():
    system, user = cw.build_dialogue_prompt("", [], None, "путешествия", "seed")
    assert "путешествия" in user
    assert system.strip()   # дефолтная персона подставлена


def test_sanitize_strips_wrappers_and_ai_disclosure():
    assert cw.sanitize_reply('"привет!"') == "привет!"
    assert cw.sanitize_reply("Макс: и правда круто") == "и правда круто"
    assert cw.sanitize_reply("строка1\nстрока2") == "строка1 строка2"
    assert cw.sanitize_reply("Как ИИ, я не могу иметь мнение") == ""   # само-раскрытие → пусто
    assert cw.sanitize_reply("") == ""


def test_sanitize_truncates_long():
    long = "предложение. " * 60
    out = cw.sanitize_reply(long)
    assert 0 < len(out) <= cw.MAX_REPLY_LEN


def test_loop_and_executor_wired():
    src = open(os.path.join(ROOT, "services", "chat_warmup.py"), encoding="utf-8").read()
    # ход диалога безопасен для сессий и осмыслен
    assert "try_claim_account" in src and "release_accounts" in src
    assert "content_safety" in src
    # каскад провайдеров: Claude → бесплатный fallback Groq/OpenRouter/Gemini
    assert "spintax_ai.complete" in src and "configured_providers" in src
    assert "async def run(" in src


def test_generate_reply_none_without_any_ai(monkeypatch):
    """Без Claude и без провайдеров — ход пропускается (None), мусор не шлём."""
    import asyncio
    from services import ai_claude, ai_providers, chat_warmup as cwm
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(ai_providers, "configured_providers", lambda: [])
    recent = [{"id": 1, "text": "привет", "is_fleet": False, "sender_name": "Аня"}]
    out = asyncio.run(cwm.generate_reply("персона", recent, None, "", "seed"))
    assert out is None


def test_generate_reply_uses_groq_fallback(monkeypatch):
    """Claude выключен, но есть провайдер (Groq) → генерируем через spintax_ai."""
    import asyncio
    from services import ai_claude, ai_providers, spintax_ai, chat_warmup as cwm
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(ai_providers, "configured_providers",
                        lambda: [object()])   # непустой список провайдеров
    async def _fake_complete(system, user):
        return "  ага, звучит норм  "
    monkeypatch.setattr(spintax_ai, "complete", _fake_complete)
    out = asyncio.run(cwm.generate_reply("персона", [], None, "болтовня", "seed"))
    assert out == "ага, звучит норм"   # сгенерировано провайдером + очищено
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "chat_warmup.run" in main   # цикл зарегистрирован


def test_schema_defines_table():
    sql = open(os.path.join(ROOT, "schema_v179.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS chat_warmup_sessions" in sql
    assert "mode" in sql and "last_seen_msg" in sql


def test_api_endpoints_and_routes():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    for fn in ("chatwarmup_sessions", "chatwarmup_create", "chatwarmup_status"):
        assert f"async def {fn}" in api
    for r in ('"/api/miniapp/chatwarmup/sessions"', '"/api/miniapp/chatwarmup/session"',
              '"/api/miniapp/chatwarmup/session/{sid}/status"'):
        assert r in api


def test_frontend_screen_and_functions():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-chatwarmup"' in html
    assert "function openChatWarmup" in html and "async function cwStart" in html
    assert 'onclick="openChatWarmup()"' in html
    # выбор режима работы модуля присутствует в UI
    for m in ("mixed", "engage", "seed"):
        assert f"cwPickMode('{m}')" in html


# ── Улучшения: реакции, выбор аккаунтов, подсказка Пульса, путь ключа ─────────

def test_reactions_pure_logic():
    assert cw.pick_reaction() in cw.REACTIONS
    # реакция только когда отвечаем реальному участнику
    assert cw.should_react("seed", True, roll=0.0) is False
    assert cw.should_react("reply", False, roll=0.0) is False
    assert cw.should_react("reply", True, roll=0.0) is True                 # низкий roll → реагируем
    assert cw.should_react("reply", True, roll=0.99) is False               # высокий roll → пишем текст


def test_executor_has_reaction_branch():
    src = open(os.path.join(ROOT, "services", "chat_warmup.py"), encoding="utf-8").read()
    assert "SendReactionRequest" in src and "should_react" in src


def test_account_selection_endpoint_and_ui():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def chatwarmup_accounts" in api
    assert '"/api/miniapp/chatwarmup/accounts"' in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "cwLoadAccounts" in html and "cwToggleAcc" in html
    assert "account_ids:[...CW_ACCS]" in html   # выбранные аккаунты уходят на бэкенд


def test_pulse_suggestion_when_chats_quiet():
    from services.organism import brain
    # есть группы, разогрев не запущен → подсказка оживить чаты
    snap = {"fleet": {"accounts": 5}, "chat_warmup": {"active": 0, "stalled": 0, "chats": 4}}
    sugs = brain.build_suggestions(snap)
    assert any(s["action"].get("kind") == "chatwarmup" for s in sugs)
    # активные простаивают → предупреждение
    snap2 = {"fleet": {"accounts": 5}, "chat_warmup": {"active": 2, "stalled": 2, "chats": 4}}
    assert any(s["id"] == "cw_stalled" for s in brain.build_suggestions(snap2))
    # world отдаёт блок chat_warmup
    world = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _chat_warmup" in world and '"chat_warmup":' in world
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "if (k==='chatwarmup') return openChatWarmup();" in html


def test_anthropic_key_path_wired():
    admin = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert '("anthropic", "Anthropic (Claude)", "ai_anthropic_key", "ANTHROPIC_API_KEY")' in admin
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert '("ANTHROPIC_API_KEY", "ai_anthropic_key")' in main


def test_ambient_keyless_flag(monkeypatch):
    """«Модели без ключа»: флаг ambient включается через override из БД/UI."""
    from services import ai_providers, ai_claude
    monkeypatch.delenv("ANTHROPIC_USE_AMBIENT", raising=False)
    ai_providers._KEY_OVERRIDES.pop("ANTHROPIC_USE_AMBIENT", None)
    assert ai_claude._ambient_allowed() is False
    ai_providers.set_ai_keys({"ANTHROPIC_USE_AMBIENT": "1"})   # тумблер ВКЛ
    try:
        assert ai_claude._ambient_allowed() is True
        assert ai_claude.enabled() is True                    # keyless → доступно
    finally:
        ai_providers.set_ai_keys({"ANTHROPIC_USE_AMBIENT": ""})  # снять override
    assert ai_claude._ambient_allowed() is False


def test_ambient_toggle_wired():
    admin = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert "adm:ai_ambient" in admin and "ai_anthropic_ambient" in admin
    assert "ANTHROPIC_USE_AMBIENT" in admin
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "ai_anthropic_ambient" in main and "ANTHROPIC_USE_AMBIENT" in main
