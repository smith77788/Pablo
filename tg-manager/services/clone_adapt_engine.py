"""«Клон и адаптация»: копия профиля бота на другие боты с переработкой.

Один движок на три входа: исполнитель операции (`op_worker._exec_clone_adapt`),
предпросмотр в мини-аппе (`/api/miniapp/clone_adapt/preview`) и мастер в боте.
Раньше их было два и оба разные: мини-апп ставил операцию, которая читала
ЗАШИФРОВАННЫЙ токен из базы как есть (то есть каждый вызов Telegram уходил с
мусорным токеном и клон не работал никогда), а чекбокс «Фото» молча
игнорировался; бот клонировал прямо в обработчике кнопки, мимо очереди
операций, без журнала и без отката.

Что умеет:

* поля — имя, описание, краткое описание, команды, фото, кнопка меню, права
  по умолчанию в группах и каналах;
* языки — кроме основного профиля, переводы имени/описаний/команд на
  выбранных языках (или на всех, что есть у источника);
* адаптация — шаблон имени (`{name}`, `{n}`, `{target}`, `{username}`),
  замены «что → на что» во всех текстах, подмена упоминаний источника
  (@ник, t.me/ник) на упоминания цели;
* «только разница» — поле, которое у цели уже такое же, не трогаем: это и
  бережёт жёсткие лимиты Telegram на setMy*, и делает повтор идемпотентным;
* снимок «как было» для отката — `snapshot_before` + `restore_calls`.

Модуль без побочных эффектов, кроме явных вызовов Telegram в `read_profile`,
`read_photo` и `apply_step`: план (`build_plan`) — чистая функция и под
тестом без сети.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ── Поля ─────────────────────────────────────────────────────────────────────

FIELDS = ("name", "desc", "short", "commands", "photo", "menu", "rights")

FIELD_LABELS = {
    "name": "Имя",
    "desc": "Описание",
    "short": "Краткое описание",
    "commands": "Команды",
    "photo": "Фото",
    "menu": "Кнопка меню",
    "rights": "Права по умолчанию",
}

# Поля, у которых бывают переводы (language_code в Bot API).
LOCALIZED = ("name", "desc", "short", "commands")

# Лимиты Telegram на длину — длиннее Bot API отбивает весь вызов.
LIMITS = {"name": 64, "desc": 512, "short": 120}
CMD_DESC_LIMIT = 256

# Языки, переводы на которые ищем у источника при «все языки». Тот же
# список, что у экрана мультигео.
KNOWN_LANGS = ("ru", "en", "de", "fr", "es", "it", "uk", "pt", "zh", "ar",
               "tr", "kk", "uz", "pl")

LANG_LABELS = {
    "": "основной", "ru": "русский", "en": "английский", "de": "немецкий",
    "fr": "французский", "es": "испанский", "it": "итальянский",
    "uk": "украинский", "pt": "португальский", "zh": "китайский",
    "ar": "арабский", "tr": "турецкий", "kk": "казахский", "uz": "узбекский",
    "pl": "польский",
}

MAX_REPLACEMENTS = 20
MAX_TARGETS = 200

# Шаблон имени по умолчанию — копия как есть.
DEFAULT_NAME_TEMPLATE = "{name}"


# ── Настройки ────────────────────────────────────────────────────────────────

def normalize_fields(raw: Any) -> list[str]:
    """Поля из запроса: строка через запятую (старый формат) или список.

    Незнакомые имена отбрасываются, порядок — канонический (`FIELDS`).
    """
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    got = set(items)
    return [f for f in FIELDS if f in got]


def normalize_settings(raw: Any) -> dict:
    """Настройки адаптации из запроса — всегда полный и безопасный словарь."""
    raw = raw if isinstance(raw, dict) else {}
    tpl = str(raw.get("name_template") or DEFAULT_NAME_TEMPLATE)[:120]
    if not tpl.strip():
        tpl = DEFAULT_NAME_TEMPLATE

    reps: list[list[str]] = []
    for pair in (raw.get("replacements") or [])[:MAX_REPLACEMENTS]:
        if isinstance(pair, dict):
            src, dst = pair.get("from"), pair.get("to")
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            src, dst = pair
        else:
            continue
        src = str(src or "")[:200]
        dst = str(dst or "")[:200]
        if src:
            reps.append([src, dst])

    langs_raw = raw.get("langs")
    if langs_raw == "all":
        langs: Any = "all"
    else:
        langs = []
        for lc in (langs_raw or []):
            lc = str(lc or "").strip().lower()
            if re.fullmatch(r"[a-z]{2,3}", lc) and lc not in langs:
                langs.append(lc)
        langs = langs[:len(KNOWN_LANGS)]

    return {
        "name_template": tpl,
        "replacements": reps,
        "swap_mentions": raw.get("swap_mentions", True) is not False,
        "skip_same": raw.get("skip_same", True) is not False,
        "langs": langs,
    }


def settings_summary(fields: list[str], settings: dict) -> str:
    """Настройки человеческим языком — для истории и подтверждения."""
    parts = [", ".join(FIELD_LABELS[f].lower() for f in fields) or "ничего"]
    tpl = settings.get("name_template") or DEFAULT_NAME_TEMPLATE
    if "name" in fields and tpl != DEFAULT_NAME_TEMPLATE:
        parts.append(f"имя по шаблону «{tpl}»")
    reps = settings.get("replacements") or []
    if reps:
        parts.append(f"замен: {len(reps)}")
    if settings.get("swap_mentions"):
        parts.append("@ники образца → ники ботов")
    langs = settings.get("langs")
    if langs == "all":
        parts.append("все переводы образца")
    elif langs:
        parts.append("языки: " + ", ".join(langs))
    if not settings.get("skip_same", True):
        parts.append("перезаписывать совпадающее")
    return "; ".join(parts)


# ── Адаптация текста ─────────────────────────────────────────────────────────

def _render(template: str, vars_: dict) -> str:
    """Подставить ТОЛЬКО известные метки. Прочие фигурные скобки в тексте
    остаются как есть: str.format уронил бы описание с «{» внутри."""
    out = template
    for key, val in vars_.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _swap_mentions(text: str, src_uname: str, tgt_uname: str) -> str:
    if not text or not src_uname or not tgt_uname:
        return text
    s = re.escape(src_uname.lstrip("@"))
    t = tgt_uname.lstrip("@")
    # @ник и t.me/ник (с https:// и без), без учёта регистра — Telegram
    # ники к регистру безразличны. Граница справа — чтобы @shop_bot не
    # задел @shop_bot2.
    text = re.sub(r"(?i)@" + s + r"(?![A-Za-z0-9_])", "@" + t, text)
    text = re.sub(r"(?i)(t(?:elegram)?\.me/)" + s + r"(?![A-Za-z0-9_])",
                  lambda m: m.group(1) + t, text)
    return text


def adapt_text(text: str, *, settings: dict, vars_: dict,
               src_uname: str = "", tgt_uname: str = "") -> str:
    """Текст источника → текст для цели: замены, упоминания, метки."""
    out = text or ""
    for src, dst in settings.get("replacements") or []:
        out = out.replace(src, dst)
    if settings.get("swap_mentions"):
        out = _swap_mentions(out, src_uname, tgt_uname)
    return _render(out, vars_)


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _norm_cmds(cmds: Any) -> list[dict]:
    out = []
    for c in cmds or []:
        if not isinstance(c, dict):
            continue
        cmd = str(c.get("command") or "").strip().lstrip("/")
        if cmd:
            out.append({"command": cmd, "description": str(c.get("description") or "")})
    return out


def _canon(v: Any) -> str:
    """Каноническая форма значения для сравнения «уже такое же»."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return "" if v is None else str(v)


def lang_suffix(lc: str) -> str:
    return f":{lc}" if lc else ""


def step_label(field: str, lc: str = "") -> str:
    lbl = FIELD_LABELS.get(field, field)
    if lc:
        lbl += f" ({LANG_LABELS.get(lc, lc)})"
    return lbl


def preview_value(v: Any, n: int = 80) -> str:
    """Значение поля — короткой строкой для экрана и журнала."""
    if isinstance(v, list):
        if not v:
            return "нет"
        return ", ".join("/" + str(c.get("command", "")) for c in v if isinstance(c, dict))[:n]
    if isinstance(v, dict):
        if not v:
            return "нет"
        if v.get("type") == "web_app":
            return f"мини-апп «{v.get('text', '')}»"[:n]
        if v.get("type") == "commands":
            return "список команд"
        if v.get("type") == "default":
            return "по умолчанию"
        on = [k for k, val in v.items() if val is True]
        return (f"включено прав: {len(on)}" if on or "can_manage_chat" in v
                else json.dumps(v, ensure_ascii=False)[:n])
    s = " ".join(str(v or "").split())
    if not s:
        return "пусто"
    return (s[:n] + "…") if len(s) > n else s


# ── План ─────────────────────────────────────────────────────────────────────

def effective_langs(settings: dict, source: dict) -> list[str]:
    """Языки, которые реально переносим: всегда основной ("") плюс выбранные.

    «all» — языки, на которых у источника заполнено хоть что-то."""
    langs = settings.get("langs")
    if langs == "all":
        extra = [lc for lc in (source.get("langs") or {}) if lc]
        extra += [lc for lc in (source.get("commands") or {})
                  if lc and lc not in extra and source["commands"][lc]]
        return [""] + extra
    return [""] + [lc for lc in (langs or []) if lc]


def build_plan(source: dict, target: dict, fields: list[str], settings: dict,
               index: int = 1) -> list[dict]:
    """Что поменяется у одной цели. Чистая функция.

    source/target — профили из `read_profile`. Возвращает шаги:
    {key, field, lang, label, before, after, same, truncated, note}.
    key — имя поля в снимке отката (совместимо с массовой правкой профиля:
    name / description / short_description для основного языка).
    """
    tgt_uname = str(target.get("username") or "")
    src_uname = str(source.get("username") or "")
    tgt_name_now = (target.get("langs", {}).get("", {}) or {}).get("name", "")
    vars_ = {
        "n": index,
        "target": tgt_name_now or tgt_uname,
        "username": tgt_uname,
    }
    plan: list[dict] = []
    langs = effective_langs(settings, source)
    skip_same = settings.get("skip_same", True)

    def _add(key, field, lc, before, after, truncated=False, note=""):
        same = _canon(before) == _canon(after)
        plan.append({
            "key": key, "field": field, "lang": lc,
            "label": step_label(field, lc),
            "before": before, "after": after,
            "same": bool(same and skip_same), "truncated": truncated, "note": note,
        })

    for lc in langs:
        s_prof = (source.get("langs") or {}).get(lc) or {}
        t_prof = (target.get("langs") or {}).get(lc) or {}
        if "name" in fields:
            src_name = s_prof.get("name", "")
            # Пустое имя основного языка у источника не копируем: это стёрло
            # бы имя цели, и Telegram показал бы её голым @ником. Пустой
            # перевод копируем — это и есть «как у источника».
            if src_name or lc:
                after = ""
                if src_name:
                    # Замены и упоминания — к самому имени источника,
                    # шаблон — поверх.
                    tpl = settings.get("name_template") or DEFAULT_NAME_TEMPLATE
                    adapted = adapt_text(src_name, settings=settings, vars_=vars_,
                                         src_uname=src_uname, tgt_uname=tgt_uname)
                    after = _render(tpl, dict(vars_, name=adapted)).strip()
                after, cut = _cut(after, LIMITS["name"])
                _add("name" + lang_suffix(lc), "name", lc,
                     t_prof.get("name", ""), after, cut)
        if "desc" in fields:
            after = adapt_text(s_prof.get("description", ""), settings=settings,
                               vars_=dict(vars_, name=s_prof.get("name", "")),
                               src_uname=src_uname, tgt_uname=tgt_uname)
            after, cut = _cut(after, LIMITS["desc"])
            _add("description" + lang_suffix(lc), "desc", lc,
                 t_prof.get("description", ""), after, cut)
        if "short" in fields:
            after = adapt_text(s_prof.get("short_description", ""), settings=settings,
                               vars_=dict(vars_, name=s_prof.get("name", "")),
                               src_uname=src_uname, tgt_uname=tgt_uname)
            after, cut = _cut(after, LIMITS["short"])
            _add("short_description" + lang_suffix(lc), "short", lc,
                 t_prof.get("short_description", ""), after, cut)
        if "commands" in fields:
            src_cmds = _norm_cmds((source.get("commands") or {}).get(lc))
            cut = False
            new_cmds = []
            for c in src_cmds:
                d = adapt_text(c["description"], settings=settings,
                               vars_=dict(vars_, name=s_prof.get("name", "")),
                               src_uname=src_uname, tgt_uname=tgt_uname)
                d, c_cut = _cut(d, CMD_DESC_LIMIT)
                cut = cut or c_cut
                new_cmds.append({"command": c["command"], "description": d})
            _add("commands" + lang_suffix(lc), "commands", lc,
                 _norm_cmds((target.get("commands") or {}).get(lc)), new_cmds, cut)

    if "menu" in fields:
        src_menu = source.get("menu_button")
        if src_menu is not None:
            _add("menu_button", "menu", "", target.get("menu_button") or {}, src_menu)
    if "rights" in fields:
        for kind, key in (("groups", "rights_groups"), ("channels", "rights_channels")):
            src_r = (source.get("rights") or {}).get(kind)
            if src_r is not None:
                _add(key, "rights", "",
                     (target.get("rights") or {}).get(kind) or {}, src_r,
                     note="в каналах" if kind == "channels" else "в группах")
                plan[-1]["label"] = "Права по умолчанию " + (
                    "в каналах" if kind == "channels" else "в группах")
    if "photo" in fields:
        if source.get("has_photo"):
            # Фото не сравниваем побайтно (у цели Telegram пережимает файл),
            # поэтому шаг всегда «применить».
            plan.append({"key": "photo", "field": "photo", "lang": "",
                         "label": FIELD_LABELS["photo"], "before": "текущее",
                         "after": "фото источника", "same": False,
                         "truncated": False, "note": ""})
        else:
            plan.append({"key": "photo", "field": "photo", "lang": "",
                         "label": FIELD_LABELS["photo"], "before": "",
                         "after": "", "same": True, "truncated": False,
                         "note": "у источника нет фото — пропущено"})
    return plan


def plan_counts(plan: list[dict]) -> dict:
    change = sum(1 for s in plan if not s["same"])
    return {"change": change, "same": len(plan) - change,
            "truncated": sum(1 for s in plan if s.get("truncated") and not s["same"])}


def public_step(step: dict) -> dict:
    """Шаг плана для экрана: значения короткими строками, без сырья."""
    return {
        "label": step["label"],
        "before": preview_value(step["before"], 160),
        "after": preview_value(step["after"], 160),
        "same": step["same"],
        "truncated": bool(step.get("truncated")),
        "note": step.get("note") or "",
    }


# ── Чтение профиля из Telegram ───────────────────────────────────────────────

async def read_profile(sess, token: str, fields: list[str], langs: list[str] | str,
                       *, username: str = "") -> dict:
    """Текущий профиль бота по выбранным полям и языкам.

    langs == "all" — пробуем все `KNOWN_LANGS` и оставляем заполненные.
    Сбой чтения одного поля — пустое значение, а не провал всего клона.
    """
    import asyncio
    from services import bot_api

    lang_list = [""] + (list(KNOWN_LANGS) if langs == "all"
                        else [lc for lc in (langs or []) if lc])
    prof: dict = {"username": username, "langs": {}, "commands": {}}

    need_text = any(f in fields for f in ("name", "desc", "short"))
    if need_text:
        rows = await bot_api.get_localized_profiles(sess, token, lang_list)
        for r in rows:
            lc = "" if r.get("lang") == "default" else r.get("lang", "")
            prof["langs"][lc] = {"name": r.get("name", ""),
                                 "description": r.get("description", ""),
                                 "short_description": r.get("short_description", "")}
        # Основной язык есть всегда, даже пустой: иначе план решил бы, что
        # у цели «нет профиля», а не «профиль пуст».
        prof["langs"].setdefault("", {"name": "", "description": "",
                                      "short_description": ""})
        if langs != "all":
            for lc in lang_list:
                prof["langs"].setdefault(lc, {"name": "", "description": "",
                                              "short_description": ""})

    if "commands" in fields:
        async def _cmds(lc):
            try:
                return lc, await bot_api.get_my_commands(sess, token, lc)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — пустой список вместо провала
                log.warning("clone_adapt: команды (%s) не прочитаны: %s", lc, e)
                return lc, []
        for lc, cmds in await asyncio.gather(*(_cmds(lc) for lc in lang_list)):
            if cmds or lc == "" or langs != "all":
                prof["commands"][lc] = _norm_cmds(cmds)

    if "menu" in fields:
        try:
            data = await bot_api._call(sess, token, "getChatMenuButton")
            prof["menu_button"] = data.get("result") if data.get("ok") else None
        except Exception as e:  # noqa: BLE001
            log.warning("clone_adapt: кнопка меню не прочитана: %s", e)
            prof["menu_button"] = None

    if "rights" in fields:
        prof["rights"] = {}
        for kind, for_channels in (("groups", False), ("channels", True)):
            try:
                data = await bot_api._call(sess, token, "getMyDefaultAdministratorRights",
                                           for_channels=for_channels)
                prof["rights"][kind] = data.get("result") if data.get("ok") else None
            except Exception as e:  # noqa: BLE001
                log.warning("clone_adapt: права (%s) не прочитаны: %s", kind, e)
                prof["rights"][kind] = None
    return prof


async def read_photo(sess, token: str) -> bytes | None:
    """Байты текущего фото бота (самый крупный размер) — или None.

    Ссылка на файл содержит токен: в лог её не пишем ни при каком исходе.
    """
    import aiohttp
    from services import bot_api

    try:
        me = await bot_api.get_me(sess, token)
        if not me:
            return None
        ph = await bot_api._call(sess, token, "getUserProfilePhotos",
                                 user_id=me.get("id"), limit=1)
        photos = (ph.get("result") or {}).get("photos") or [] if ph.get("ok") else []
        if not photos or not photos[0]:
            return None
        file_id = photos[0][-1]["file_id"]
        f = await bot_api._call(sess, token, "getFile", file_id=file_id)
        if not f.get("ok"):
            return None
        path = (f.get("result") or {}).get("file_path")
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{token}/{path}"
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            return data if data else None
    except Exception as e:  # noqa: BLE001 — фото best-effort
        log.warning("clone_adapt: фото источника не скачано (%s)", type(e).__name__)
        return None


# ── Применение и откат ───────────────────────────────────────────────────────

def _call_for(key: str, value: Any) -> tuple[str, dict]:
    """(метод Bot API, параметры) — поставить значение `value` по ключу снимка."""
    base, _, lc = key.partition(":")
    lang = {"language_code": lc} if lc else {}
    if base == "name":
        return "setMyName", {"name": value or "", **lang}
    if base == "description":
        return "setMyDescription", {"description": value or "", **lang}
    if base == "short_description":
        return "setMyShortDescription", {"short_description": value or "", **lang}
    if base == "commands":
        if not value:
            return "deleteMyCommands", dict(lang)
        return "setMyCommands", {"commands": value, **lang}
    if base == "menu_button":
        return "setChatMenuButton", {"menu_button": value or {"type": "default"}}
    if base == "rights_groups":
        return "setMyDefaultAdministratorRights", {"rights": value or None,
                                                    "for_channels": False}
    if base == "rights_channels":
        return "setMyDefaultAdministratorRights", {"rights": value or None,
                                                    "for_channels": True}
    raise KeyError(key)


RESTORABLE_PREFIXES = ("name", "description", "short_description", "commands",
                       "menu_button", "rights_groups", "rights_channels")


def is_restorable(key: str) -> bool:
    return key.partition(":")[0] in RESTORABLE_PREFIXES


def snapshot_before(plan: list[dict]) -> dict:
    """Снимок «как было» только по полям, которые реально изменятся.

    Формат — тот же `bulk_edit_snapshots.before`, что у массовой правки
    профиля: откат у них общий. Фото в снимок не попадает — вернуть прежнее
    фото Bot API не позволяет (его файл недоступен после замены).
    """
    return {s["key"]: s["before"] for s in plan
            if not s["same"] and is_restorable(s["key"])}


def restore_calls(before: dict) -> list[tuple[str, str, dict]]:
    """Вызовы, возвращающие снимок: [(подпись, метод, параметры)]."""
    out = []
    for key, val in (before or {}).items():
        if not is_restorable(key):
            continue
        base, _, lc = key.partition(":")
        field = {"description": "desc", "short_description": "short",
                 "menu_button": "menu", "rights_groups": "rights",
                 "rights_channels": "rights"}.get(base, base)
        method, params = _call_for(key, val)
        out.append((step_label(field, lc), method, params))
    return out


async def apply_step(sess, token: str, step: dict, photo: bytes | None = None) -> dict:
    """Применить один шаг плана. Возвращает сырой ответ Telegram (dict)."""
    from services import bot_api

    if step["field"] == "photo":
        if not photo:
            return {"ok": False, "description": "у источника нет фото"}
        ok = await bot_api.set_photo(sess, token, photo)
        return {"ok": bool(ok)} if ok else {"ok": False,
                                            "description": "Telegram не принял фото"}
    method, params = _call_for(step["key"], step["after"])
    return await bot_api._call(sess, token, method, **params)


def restored_label(key: str, value: Any) -> str:
    """«Команды (английский): /start, /help» — что вернул откат, для журнала."""
    base, _, lc = key.partition(":")
    field = {"description": "desc", "short_description": "short",
             "menu_button": "menu", "rights_groups": "rights",
             "rights_channels": "rights"}.get(base, base)
    return f"{step_label(field, lc)}: «{preview_value(value)}»"
