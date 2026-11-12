"""Модуль «Профили»: оформление флота и персоны.

Что здесь закреплено и почему (каждое — настоящая находка аудита 10.11.2026):

* Пустое поле на экране «Оформление» стирало фамилию и описание у ВСЕГО
  флота: исполнитель звал ``UpdateProfileRequest(last_name="", about="")``,
  хотя экран обещал «заполните хотя бы одно поле». Теперь экран шлёт
  ``keep_empty`` и пустое поле не трогается.
* Аватар был один на весь флот — одно лицо у сотни аккаунтов. Теперь набор,
  раздаётся по кругу.
* Повтор операции (сетевой сбой, деплой) проходил список заново: аватар
  ДОБАВЛЯЕТ фото, то есть аккаунт получал второе фото; смена 2FA падала
  «неверный пароль». Теперь исполнитель пропускает отработанные аккаунты.
* Пароль 2FA, поставленный массово, нигде не сохранялся — потерял, значит
  потерял флот. Теперь он лежит у аккаунта зашифрованным.
* Персоны, созданные в мини-аппе, не привязывались к аккаунту, а прогрев и
  ИИ-комментинг ищут персону ПО АККАУНТУ — ни одна не говорила нигде.
* Промпт персоны отдавал модели ключ стиля («Стиль речи: casual.»).
"""
from __future__ import annotations

import pytest

from services import profile_setter_engine as pse
from tests.miniapp_source import api_function


# ── Оформление: ввод и шаблоны ──────────────────────────────────────────────

def test_empty_name_fields_are_left_untouched_not_wiped():
    params = pse.build_setter_params("name", {"about": "Люблю кофе"})
    assert params["keep_empty"] is True
    kw = pse.name_bio_kwargs(params["name_data"], params)
    assert kw["last_name"] is None, "пустая фамилия стёрла бы фамилии всего флота"
    assert kw["about"] == "Люблю кофе"


def test_old_queued_operations_keep_their_clearing_behaviour():
    """Операции, поставленные до правки (и карточка аккаунта), флага не несут:
    там пустое поле по-прежнему очищается."""
    kw = pse.name_bio_kwargs({"first_name": "Иван", "last_name": "", "about": ""}, {})
    assert kw["last_name"] == "" and kw["about"] == ""


def test_name_requires_at_least_one_field():
    with pytest.raises(ValueError):
        pse.build_setter_params("name", {"first_name": " ", "last_name": "", "about": ""})


def test_avatar_set_is_dealt_round_robin():
    params = pse.build_setter_params(
        "avatar", {"avatar_urls": "https://a/1.jpg\nhttps://a/2.jpg\nhttps://a/3.jpg"})
    got = [pse.pick_avatar_url(params, i) for i in range(6)]
    assert got == ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"] * 2


def test_single_avatar_from_old_operations_still_works():
    assert pse.pick_avatar_url({"avatar_url": "https://a/x.jpg"}, 5) == "https://a/x.jpg"


def test_avatar_links_must_be_http():
    with pytest.raises(ValueError):
        pse.build_setter_params("avatar", {"avatar_urls": ["file:///etc/passwd"]})


def test_username_template_gives_distinct_valid_names():
    params = pse.build_setter_params("username", {"username": "alex_{n}"})
    names = {pse.render_username(params["username"], i) for i in range(20)}
    assert len(names) == 20, "юзернейм уникален в Telegram — одинаковый получит отказ"


def test_username_template_rejects_bad_shape():
    with pytest.raises(ValueError):
        pse.build_setter_params("username", {"username": "1ab"})


def test_preview_flags_same_for_everyone_and_overflow():
    out = pse.render_preview({"first_name": "Иван", "about": "x" * 90})
    assert out["distinct"] == 1
    assert "Описание" in out["too_long"]
    out = pse.render_preview({"first_name": "{А|Б|В|Г|Д|Е|Ж|З}"}, draws=40)
    assert out["distinct"] > 1


def test_every_screen_op_has_a_russian_label():
    for op in pse.SETTER_OPS:
        label = pse.setter_label(op, {"op": op, "privacy_key": "phone"}, 3)
        assert "× 3 акк." in label and op not in label.split(":", 1)[1]


@pytest.mark.asyncio
async def test_apply_op_passes_slot_to_avatar_and_username(monkeypatch):
    seen = {}

    async def _avatar(sess, acc, url):
        seen["avatar"] = url
        return {"ok": True}

    async def _uname(sess, acc, name):
        seen["username"] = name
        return {"ok": True}

    monkeypatch.setattr(pse, "set_avatar_from_url", _avatar)
    monkeypatch.setattr(pse, "set_username", _uname)
    await pse.apply_op("s", {}, "avatar", {"avatar_urls": ["https://a/1", "https://a/2"]}, slot=1)
    await pse.apply_op("s", {}, "username", {"username": "bob_{n}"}, slot=4)
    assert seen == {"avatar": "https://a/2", "username": "bob_5"}


# ── Исполнитель: повтор и 2FA ───────────────────────────────────────────────

def _executor_src() -> str:
    import inspect
    from services import op_worker
    return inspect.getsource(op_worker._exec_bulk_set_profile)


def test_executor_resumes_instead_of_redoing_accounts():
    src = _executor_src()
    assert "completed_targets(pool, op_id)" in src
    assert "_journal_done(" in src, "без записи успеха повтор не знает, что пропустить"


def test_executor_remembers_twofa_and_reuses_it():
    src = _executor_src()
    assert "_remember_twofa(" in src and "_stored_twofa(" in src


@pytest.mark.asyncio
async def test_twofa_is_stored_encrypted_and_scoped_to_owner():
    from services import op_worker

    calls = []

    class _Pool:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    await op_worker._remember_twofa(_Pool(), 7, 42, "s3cret")
    sql, args = calls[0]
    assert "owner_id=$3" in sql
    assert args[0] != "s3cret" and args[0].startswith("ENC:")
    from services.token_vault import decrypt_token
    assert decrypt_token(args[0]) == "s3cret"


def test_twofa_column_is_masked_in_api_answers():
    from services.secret_masking import SENSITIVE_FIELD_NAMES
    assert "twofa_enc" in SENSITIVE_FIELD_NAMES


# ── Персоны ─────────────────────────────────────────────────────────────────

def test_persona_create_binds_an_account():
    body = api_function("persona_create")
    assert "_persona_account(" in body and "account_id" in body, (
        "персона без аккаунта не участвует ни в одном диалоге")


def test_persona_update_exists_and_is_owner_scoped():
    body = api_function("persona_update")
    assert "owner_id=$2" in body


def test_persona_prompt_speaks_russian_not_style_keys():
    from services import persona_engine as pe
    p = pe.PersonaProfile(persona_id=1, account_id=1, name="Олег", bio="Трейдер",
                          age=30, interests=["рынки"], speech_style="casual",
                          tone="skeptical", niche="крипто", backstory="", created_at=None)
    prompt = pe.build_persona_system_prompt(p)
    assert "casual" not in prompt and "skeptical" not in prompt
    assert pe.STYLE_PROMPTS["casual"] in prompt and pe.TONE_PROMPTS["skeptical"] in prompt


def test_ai_comment_takes_the_account_persona():
    from services import ai_comment_engine as ace
    system, _ = ace.build_comment_prompt("пост", persona="Ты Олег, скептик.")
    assert system.startswith("Твой образ: Ты Олег")
    # запреты по-прежнему последние
    assert system.rstrip().endswith("Только текст комментария.")
    import inspect
    from services import op_worker
    assert "persona_voice.load" in inspect.getsource(op_worker._exec_ai_comment)
