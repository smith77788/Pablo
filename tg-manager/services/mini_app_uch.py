"""Хранилище контактов (Unified Contacts Hub) — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 79 маршрутов `/api/miniapp/uch/*` и
81 функция лежали вперемешку с флотом, облаком и A/B — в файле на 25 тысяч
строк. Это самый крупный связный блок приложения: контакты, сегменты, группы,
CRM, дубликаты, смарт-теги, массовые действия по списку, выгрузки, граф связей.

Логика живёт в пакете `services/contacts_hub/*` (репозиторий, движки поиска,
слияния, доверия, тегов), здесь — только разбор запроса, скоуп владельца,
русские ошибки и вызов движка. Поведение при переносе не менялось.

Срез контактов (фильтры, потолок запуска, гейт давления) — общий модуль
`services/contacts_hub/segments.py`: тем же срезом пользуются рассылка по
кампании и оценка аудитории инвайта.
"""
from __future__ import annotations

import json as _json
import re
import time

from aiohttp import web

# id контакта — UUID. Проверяем форму ДО запроса: asyncpg биндит uuid без
# каста, и одна некорректная строка в списке роняет весь запрос, то есть всю
# выгрузку, а не одну строку.
_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты хранилища контактов на приложении мини-аппа."""
    from services.contacts_hub import segments as _segments
    # На уровне setup_routes, а не внутри обработчика: `except BadDate` в
    # обработчике вычисляется и тогда, когда до ленивого импорта дело не
    # дошло, и неразрешённое имя превратило бы любую раннюю ошибку в NameError.
    from services.contacts_hub.repository import BadDate
    from services.mini_app_api import (
        _BAD_BODY, _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _safe_count,
        _safe_fetch, _safe_fetchrow, alive_accounts, log,
        read_upload, read_upload_text, UploadTooLarge,
        bad_body_response,
    )
    from services.security import (
        check_sql_suspicious, validate_integer, validate_string,
    )

    # Предел одной медиа-загрузки. Был записан числом в двух ручках, и в них
    # же проверялся ПОСЛЕ полного чтения поля — то есть не действовал.
    _UCH_MEDIA_MAX = 50 * 1024 * 1024
    # Фильтры и список контактов приходят полем формы как JSON: тысяча
    # идентификаторов это около сорока килобайт, мегабайта хватает с запасом.
    _UCH_JSON_MAX = 1024 * 1024

    _SEG_MAX = _segments.SEG_MAX
    _SEG_DM_CHUNK = _segments.SEG_DM_CHUNK

    # Делегаты под имена, под которыми эти помощники звались в монолите:
    # перенос обработчиков обязан быть дословным, иначе в 1600 строках не
    # отличить переезд от правки.
    def _segment_filters(src: dict) -> dict:
        return _segments.segment_filters(src)

    async def _segment_resolve(uid: int, body: dict) -> tuple[list, int]:
        return await _segments.resolve(pool, uid, body, limit=_SEG_MAX)

    async def _segment_pressure_ok(uid: int, op_name: str) -> tuple[bool, str]:
        return await _segments.pressure_ok(pool, uid, op_name)

    async def _alive_accounts(uid: int, wanted: list[int] | None = None) -> list[int]:
        return await alive_accounts(pool, uid, wanted)

    def _contact_filters(request: web.Request) -> dict:
        """Фильтры списка контактов — ОДИН разбор на список и на экспорт.

        Экспорт умел только «все контакты», хотя у хаба богатый фильтр: стадия,
        тег, группа, аккаунт, пол, личные. Владелец, собравший нужный срез, не
        мог выгрузить именно его. Если бы экспорт разбирал параметры своим
        кодом, «вижу одно — выгружаю другое» появилось бы на первой же правке
        фильтра, поэтому разбор здесь общий.
        """
        gender = request.query.get('gender') or None       # 'm' | 'f' | 'unknown'
        if gender not in ('m', 'f', 'unknown'):
            gender = None
        return dict(
            search=request.query.get('search', ''),
            tag=request.query.get('tag'),
            favorite_only=request.query.get('favorite') == '1',
            premium_only=request.query.get('premium') == '1',
            multi_only=request.query.get('multi') == '1',
            mutual_only=request.query.get('mutual') == '1',
            # Плитки «📱 Телефон» и «С ником» показывали числа, которые ни во
            # что не открывались: срез существовал только в счётчике.
            with_phone_only=request.query.get('with_phone') == '1',
            with_username_only=request.query.get('with_username') == '1',
            gender=gender,
            crm_stage=(request.query.get('crm_stage') or '').strip()[:40] or None,
            excluded_only=request.query.get('excluded_only') == '1',
            # Фильтр по группе контактов: репозиторий его умел, а список не
            # читал, поэтому группы было некуда открыть.
            group_id=validate_integer(request.query.get('group_id'), min_val=1),
            # Фильтр «контакты этого аккаунта»: фронт его отправлял, репозиторий
            # умел, а список не читал — экран обещал контакты одного аккаунта и
            # показывал весь справочник (тот же класс, что срез «Прокси мёртв»).
            account_id=validate_integer(request.query.get('account_id'), min_val=1),
        )

    async def _own_contact(cid: str, uid: int) -> bool:
        """Принадлежит ли контакт владельцу.

        НАХОДКА. Лента карточки и граф идентичности читали ПОДТАБЛИЦЫ контакта
        (contact_sources, contact_crm, last_active_at) по одному contact_id, без
        owner_id вообще: по чужому uuid отдавались аккаунты-источники, стадия
        CRM и «был сегодня». Часть запросов в тех же обработчиках владельца
        проверяла, часть — нет, поэтому ответ выглядел правдоподобно
        (полупустая лента), а не как отказ.

        Проверка одна и ДО чтения: у подтаблиц contact_sources своего owner_id
        нет вообще, поэтому скоуп может держать только сам контакт.
        """
        if not _UUID_RE.match(str(cid or '')):
            return False
        return bool(await pool.fetchval(
            'SELECT 1 FROM unified_contacts WHERE id=$1 AND owner_id=$2', cid, uid))

    def _filters_are_set(filters: dict) -> bool:
        """Есть ли хоть один фильтр: «весь хаб» и «срез» — разные выгрузки."""
        return any(v not in (None, '', False) for v in filters.values())

    async def uch_contacts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        filters = _contact_filters(request)
        # Пагинация: без неё список молча обрезался дефолтным limit=100 — при 2.9к+
        # контактов пользователь видел «лишь десятки» и не мог долистать до
        # остальных. limit зажат, offset — для «Показать ещё».
        try:
            limit = min(max(int(request.query.get('limit', 200)), 1), 500)
        except (TypeError, ValueError):
            limit = 200
        try:
            offset = max(int(request.query.get('offset', 0)), 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            from services.contacts_hub.repository import get_contacts
            result = await get_contacts(pool, uid, limit=limit, offset=offset,
                                        **filters)
            result['offset'] = offset
            result['limit'] = limit
            return _json_resp(result)
        except Exception:
            log.exception("uch_contacts")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            from services.contacts_hub.repository import get_contact
            result = await get_contact(pool, contact_id, uid)
            if not result: return _err("Не найдено", 404)
            # Авто-обогащение (чистое, на чтение): активность + предполагаемый язык.
            try:
                from services.contacts_hub import enrich as _enr
                if isinstance(result.get("contact"), dict):
                    result["enriched"] = _enr.enrich(result["contact"])
            except Exception:
                pass
            return _json_resp(result)
        except Exception:
            log.exception("uch_contact_detail")
            return _err(_INTERNAL_ERROR, 500)

    def _uch_msg_ref(c: dict) -> str | None:
        """Куда писать в ЛС: @username надёжнее всего, иначе числовой ID (send_dm
        сам резолвит цифры в user_id). Телефон для ЛС ненадёжен (нужно, чтобы
        номер уже был контактом аккаунта) — его для сообщений не используем."""
        u = (c.get("username") or "").lstrip("@")
        if u:
            return u
        if c.get("telegram_user_id"):
            return str(c["telegram_user_id"])
        return None

    def _uch_invite_target(c: dict) -> tuple[str | None, str | None]:
        """Кого приглашать: ('user', @username|id) или ('phone', номер).

        Порядок и причина — services/contacts_hub/invite_target: номер раньше
        голого id, чтобы контакт мог пригласить любой аккаунт флота, а не только
        тот, у кого он уже в книге контактов."""
        from services.contacts_hub.invite_target import invite_target
        return invite_target(c)

    async def uch_contact_message(request: web.Request) -> web.Response:
        """Написать контакту в ЛС с выбранных аккаунтов (одного/нескольких).

        Реюзает безопасный DM-движок (op bulk_dm_adhoc): темп/флуд-контроль,
        spintax-вариативность, round-robin по аккаунтам — как массовая рассылка,
        но на одного получателя. Возвращает op_id (прогресс — в «Операциях»)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        from services.contacts_hub.repository import get_contact
        res = await get_contact(pool, contact_id, uid)
        if not res:
            return _err("Контакт не найден", 404)
        ref = _uch_msg_ref(res["contact"])
        if not ref:
            return _err("У контакта нет @username или Telegram ID — писать некуда", 400)
        text = validate_string(body.get("text"), max_len=4096)
        if not text:
            return _err("Введите текст сообщения", 400)
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте", 400)
        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        delay = min(max(validate_integer(body.get("delay", 30), min_val=5, max_val=600) or 30, 5), 600)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_dm_adhoc",
                {"account_ids": acc_ids, "usernames": [ref], "text": text, "delay": delay},
                total_items=1,
                label=f"ЛС контакту @{ref} × {len(acc_ids)} акк.")
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_contact_message uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_media(request: web.Request) -> web.Response:
        """Отправить контакту МЕДИА (фото/видео/док) с подписью выбранными
        аккаунтами. Файл грузится multipart, сохраняется во временный файл, а
        отправку делает тот же DM-движок (op bulk_dm_adhoc, media-ветка): темп,
        уникализация под получателя, round-robin."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        contact_id = request.match_info['contact_id']
        try:
            reader = await request.multipart()
        except Exception:
            return _err("Ожидался файл (multipart/form-data)", 400)
        filename = "media"
        data = b""
        caption = ""
        acc_raw = ""
        try:
            async for field in reader:
                if field.name == "file":
                    filename = field.filename or "media"
                    # Предел действует НА ЧТЕНИИ: `field.read()` втянул бы поле
                    # целиком, и проверка длины после этого уже ничего не
                    # защищает — память израсходована.
                    data = await read_upload(field, _UCH_MEDIA_MAX)
                elif field.name == "text":
                    caption = (await read_upload_text(field))[:1024]
                elif field.name == "account_ids":
                    acc_raw = (await read_upload_text(field)).strip()
        except UploadTooLarge:
            return _err("Файл слишком большой (лимит 50 МБ)", 413)
        except Exception:
            return _err("Не удалось прочитать загрузку", 400)
        if not data:
            return _err("Файл пуст", 400)
        from services.contacts_hub.repository import get_contact
        res = await get_contact(pool, contact_id, uid)
        if not res:
            return _err("Контакт не найден", 404)
        ref = _uch_msg_ref(res["contact"])
        if not ref:
            return _err("У контакта нет @username или Telegram ID — писать некуда", 400)
        acc_list = [int(x) for x in re.split(r"[,\s]+", acc_raw) if x.strip().isdigit()]
        acc_ids = await _alive_accounts(uid, acc_list)
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        import os as _os
        import tempfile as _tf
        ext = _os.path.splitext(filename)[1][:12]
        try:
            fd, path = _tf.mkstemp(prefix="cdm_", suffix=ext or ".bin")
            with _os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            log.exception("uch_contact_media save uid=%s", uid)
            return _err("Не удалось сохранить файл", 500)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_dm_adhoc",
                {"account_ids": acc_ids, "usernames": [ref], "text": caption,
                 "media_path": path, "media_filename": filename, "delay": 30},
                total_items=1,
                label=f"Медиа контакту @{ref} × {len(acc_ids)} акк.")
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            try: _os.unlink(path)
            except Exception: pass
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            try: _os.unlink(path)
            except Exception: pass
            log.exception("uch_contact_media uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_invite(request: web.Request) -> web.Response:
        """Пригласить контакт в канал/чат выбранными аккаунтами.

        Реюзает движок инвайта (op mass_invite, source=import_list) на списке из
        одного получателя — способ инвайта direct/admin/link на выбор, тот же
        путь и защита от бана, что у массового инвайта."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        group = (body.get("group") or "").strip()
        if not group:
            return _err("Укажите канал/чат (@username или ссылку)", 400)
        from services.contacts_hub.repository import get_contact
        res = await get_contact(pool, contact_id, uid)
        if not res:
            return _err("Контакт не найден", 404)
        kind, ref = _uch_invite_target(res["contact"])
        if not ref:
            return _err("У контакта нет @username, ID или телефона — некого приглашать", 400)
        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        method = str(body.get("invite_method") or "direct").strip().lower()
        if method not in ("direct", "admin", "link"):
            method = "direct"
        params = {"group": group, "source": "import_list", "pace": "normal",
                  "batch_size": 1, "invite_method": method, "account_ids": acc_ids}
        if kind == "phone":
            params["phones"] = [ref]
        else:
            params["user_refs"] = [ref]
        # Тот же движок mass_invite, тот же фолбэк «недостижимым — ссылка в ЛС»
        # (включён по умолчанию в исполнителе) — пробрасываем на паритете с
        # основным экраном массового инвайта.
        if body.get("link_fallback") is False:
            params["link_fallback"] = False
        if body.get("link_message") and (method == "link" or body.get("link_fallback") is not False):
            params["link_message"] = str(body.get("link_message"))[:500]
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "mass_invite", params, total_items=1,
                label=f"Инвайт контакта {ref} → {group}")
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_contact_invite uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segment_preview(request: web.Request) -> web.Response:
        """Превью до действия: размер среза, сколько адресуемо в ЛС/инвайт,
        сколько активных аккаунтов. Живого Telegram не трогает."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            contacts, total = await _segment_resolve(uid, body)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception:
            # Сбой базы раньше приходил сюда пустым списком и печатался как
            # «в сегменте 0» — пустой экран вместо ошибки.
            log.exception("uch_segment_preview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        dm = sum(1 for c in contacts if _uch_msg_ref(c))
        inv = sum(1 for c in contacts if _uch_invite_target(c)[1])
        acc_ids = await _alive_accounts(uid, None)
        return _json_resp({
            # total — размер СРЕЗА, а не длина выдачи: за один запуск берём не
            # больше cap, и остаток (omitted) экран обязан показать. Иначе
            # «в сегменте 20000» на пятидесяти тысячах читается как «все».
            "total": total, "resolved": len(contacts), "cap": _SEG_MAX,
            "omitted": max(0, total - len(contacts)),
            "addressable_dm": dm, "addressable_invite": inv,
            "skipped_dm": len(contacts) - dm, "accounts_active": len(acc_ids),
            "dm_chunks": (dm + _SEG_DM_CHUNK - 1) // _SEG_DM_CHUNK if dm else 0,
        })

    async def uch_segment_message(request: web.Request) -> web.Response:
        """Написать в ЛС всему сегменту выбранными аккаунтами. Авто-разбивка на
        операции по ≤1000 получателей (bulk_dm_adhoc): темп/флуд/spintax."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        # A/B: если передан список variants (2–4 текста) — раскладка по вариантам.
        from services import ab_engine
        variants = ab_engine.clean_variants(body.get("variants"))
        text = validate_string(body.get("text"), max_len=4096)
        if not variants and not text:
            return _err("Введите текст сообщения", 400)
        for _t in (variants or [text]):
            if check_sql_suspicious(_t):
                return _err("Недопустимые символы в тексте", 400)
        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        try:
            contacts, seg_total = await _segment_resolve(uid, body)
        except ValueError as exc:
            return _err(str(exc), 400)
        # omitted — те, кто в срез попал, но за этот запуск НЕ поедет (потолок
        # _SEG_MAX). Молчать об этом нельзя: тост «запущено N получателей»
        # читается как «сегмент разослан».
        refs = [r for r in (_uch_msg_ref(c) for c in contacts) if r]
        if not refs:
            return _err("В сегменте нет адресуемых контактов (нужен @username или ID)", 400)
        ready, reason = await _segment_pressure_ok(uid, "bulk_dm_adhoc")
        if not ready:
            return _err(f"Инфраструктура перегружена: {reason}", 429)
        # Консервативный темп по умолчанию (реже — безопаснее).
        delay = min(max(validate_integer(body.get("delay", 45), min_val=5, max_val=600) or 45, 5), 600)
        # Список (вариант_текст, метка_варианта, его_получатели). Без A/B — один
        # «вариант» со всей аудиторией. С A/B — аудитория делится поровну.
        # ab_batch — общий id для операций одного A/B-теста (для сводки результатов).
        # мс-гранулярность: два A/B-запуска в одну секунду не сольются в один батч
        ab_batch = int(time.time() * 1000) if variants else None
        if variants:
            groups = ab_engine.split_audience(refs, len(variants))
            plan = [(variants[i], f"A/B#{i + 1}", groups[i]) for i in range(len(variants))]
        else:
            plan = [(text, "", refs)]
        try:
            from services import operation_bus
            op_ids = []
            for vtext, vlabel, vrefs in plan:
                for i in range(0, len(vrefs), _SEG_DM_CHUNK):
                    chunk = vrefs[i:i + _SEG_DM_CHUNK]
                    if not chunk:
                        continue
                    tag = (vlabel + " ") if vlabel else ""
                    oid = await operation_bus.submit(
                        pool, uid, "bulk_dm_adhoc",
                        {"account_ids": acc_ids, "usernames": chunk, "text": vtext,
                         "delay": delay, "ab_variant": vlabel or None,
                         "ab_batch": ab_batch},
                        total_items=len(chunk),
                        label=f"{tag}Сегмент ЛС ч.{i // _SEG_DM_CHUNK + 1}: {len(chunk)} × {len(acc_ids)} акк.")
                    op_ids.append(oid)
            return _json_resp({"ok": True, "op_ids": op_ids, "chunks": len(op_ids),
                               "recipients": len(refs), "skipped": len(contacts) - len(refs),
                               "segment_total": seg_total,
                               "omitted": max(0, seg_total - len(contacts)),
                               "accounts": len(acc_ids), "variants": len(variants) or 1,
                               "ab_batch": ab_batch})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_segment_message uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segment_media(request: web.Request) -> web.Response:
        """Медиа всему сегменту. Multipart: file + text + account_ids + фильтры.
        Авто-разбивка: КАЖДЫЙ чанк получает свою копию файла (op удаляет свою)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            reader = await request.multipart()
        except Exception:
            return _err("Ожидался файл (multipart/form-data)", 400)
        filename = "media"; data = b""; caption = ""; acc_raw = ""; body: dict = {}
        try:
            async for field in reader:
                if field.name == "file":
                    filename = field.filename or "media"
                    data = await read_upload(field, _UCH_MEDIA_MAX)
                elif field.name == "text":
                    caption = (await read_upload_text(field))[:1024]
                elif field.name == "account_ids":
                    acc_raw = (await read_upload_text(field)).strip()
                elif field.name == "filters":
                    try: body = _json.loads(await read_upload_text(field, _UCH_JSON_MAX))
                    except Exception: body = {}
                elif field.name == "contact_ids":
                    try: body["contact_ids"] = _json.loads(await read_upload_text(field, _UCH_JSON_MAX))
                    except Exception: pass
        except UploadTooLarge:
            return _err("Файл слишком большой (лимит 50 МБ)", 413)
        except Exception:
            return _err("Не удалось прочитать загрузку", 400)
        if not data:
            return _err("Файл пуст", 400)
        acc_ids = await _alive_accounts(
            uid, [int(x) for x in re.split(r"[,\s]+", acc_raw) if x.strip().isdigit()])
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        try:
            contacts, seg_total = await _segment_resolve(uid, body)
        except ValueError as exc:
            return _err(str(exc), 400)
        # omitted — те, кто в срез попал, но за этот запуск НЕ поедет (потолок
        # _SEG_MAX). Молчать об этом нельзя: тост «запущено N получателей»
        # читается как «сегмент разослан».
        refs = [r for r in (_uch_msg_ref(c) for c in contacts) if r]
        if not refs:
            return _err("В сегменте нет адресуемых контактов (нужен @username или ID)", 400)
        ready, reason = await _segment_pressure_ok(uid, "bulk_dm_adhoc")
        if not ready:
            return _err(f"Инфраструктура перегружена: {reason}", 429)
        import os as _os, tempfile as _tf
        ext = _os.path.splitext(filename)[1][:12] or ".bin"
        try:
            from services import operation_bus
            op_ids = []
            for i in range(0, len(refs), _SEG_DM_CHUNK):
                chunk = refs[i:i + _SEG_DM_CHUNK]
                fd, path = _tf.mkstemp(prefix="cseg_", suffix=ext)  # своя копия на чанк
                with _os.fdopen(fd, "wb") as f:
                    f.write(data)
                oid = await operation_bus.submit(
                    pool, uid, "bulk_dm_adhoc",
                    {"account_ids": acc_ids, "usernames": chunk, "text": caption,
                     "media_path": path, "media_filename": filename, "delay": 45},
                    total_items=len(chunk),
                    label=f"Сегмент медиа ч.{i // _SEG_DM_CHUNK + 1}: {len(chunk)} × {len(acc_ids)} акк.")
                op_ids.append(oid)
            return _json_resp({"ok": True, "op_ids": op_ids, "chunks": len(op_ids),
                               "recipients": len(refs), "accounts": len(acc_ids),
                               "segment_total": seg_total,
                               "omitted": max(0, seg_total - len(contacts))})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_segment_media uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segment_invite(request: web.Request) -> web.Response:
        """Пригласить весь сегмент в канал/чат. mass_invite (source=import_list)
        принимает списки нативно — авто-разбивка не нужна."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        group = (body.get("group") or "").strip()
        if not group:
            return _err("Укажите канал/чат (@username или ссылку)", 400)
        acc_ids = await _alive_accounts(
            uid, [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()])
        if not acc_ids:
            return _err("Выберите хотя бы один активный аккаунт", 400)
        try:
            contacts, seg_total = await _segment_resolve(uid, body)
        except ValueError as exc:
            return _err(str(exc), 400)
        # omitted — те, кто в срез попал, но за этот запуск НЕ поедет (потолок
        # _SEG_MAX). Молчать об этом нельзя: тост «запущено N получателей»
        # читается как «сегмент разослан».
        user_refs, phones = [], []
        for c in contacts:
            kind, ref = _uch_invite_target(c)
            if kind == "user":
                user_refs.append(ref)
            elif kind == "phone":
                phones.append(ref)
        if not user_refs and not phones:
            return _err("В сегменте некого приглашать (нет @username/ID/телефона)", 400)
        method = str(body.get("invite_method") or "direct").strip().lower()
        if method not in ("direct", "admin", "link"):
            method = "direct"
        params = {"group": group, "source": "import_list", "pace": "normal",
                  "batch_size": 5, "invite_method": method, "account_ids": acc_ids}
        if user_refs:
            params["user_refs"] = user_refs
        if phones:
            params["phones"] = phones
        # Тот же движок mass_invite, тот же фолбэк «недостижимым — ссылка в ЛС»
        # (включён по умолчанию в исполнителе) — пробрасываем на паритете с
        # основным экраном массового инвайта.
        if body.get("link_fallback") is False:
            params["link_fallback"] = False
        if body.get("link_message") and (method == "link" or body.get("link_fallback") is not False):
            params["link_message"] = str(body.get("link_message"))[:500]
        n_targets = len(user_refs) + len(phones)
        # Единый каркас: ban-safety гейт + постановка + событие в организм.
        from services import module_kit
        try:
            op_id = await module_kit.submit_guarded(
                pool, uid, "mass_invite", params, total_items=n_targets,
                label=f"Сегмент-инвайт → {group}: {n_targets} × {len(acc_ids)} акк.")
            return _json_resp({"ok": True, "op_id": op_id,
                               "targets": n_targets, "accounts": len(acc_ids),
                               "segment_total": seg_total,
                               "omitted": max(0, seg_total - len(contacts))})
        except module_kit.OpGateError as exc:
            return _err(f"Инфраструктура перегружена: {exc}", 429)
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_segment_invite uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segments_list(request: web.Request) -> web.Response:
        """Сохранённые сегменты владельца (+ актуальный размер каждого)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.repository import list_segments
            return _json_resp({"segments": await list_segments(pool, uid)})
        except Exception:
            log.exception("uch_segments_list uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segment_save(request: web.Request) -> web.Response:
        """Сохранить текущий срез как сегмент (имя + фильтры)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = (body.get("name") or "").strip()
        if not name:
            return _err("Укажите название сегмента", 400)
        filters = _segment_filters(body.get("filters") or body)
        try:
            from services.contacts_hub.repository import save_segment
            sid = await save_segment(pool, uid, name, filters)
            return _json_resp({"ok": True, "id": sid})
        except Exception:
            log.exception("uch_segment_save uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_segment_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = int(request.match_info["seg_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сегмента", 400)
        from services.contacts_hub.repository import delete_segment
        return _json_resp({"ok": await delete_segment(pool, uid, sid)})

    async def uch_intent_rules(request: web.Request) -> web.Response:
        """Правила сенсора намерений (Vault → CRM) владельца."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import intent_sensor
        try:
            rules = await intent_sensor.list_rules(pool, uid)
            return _json_resp({"rules": rules, "stages": list(intent_sensor.VALID_STAGES)})
        except Exception:
            log.exception("uch_intent_rules uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_intent_rule_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        from services import intent_sensor
        try:
            rid = await intent_sensor.add_rule(
                pool, uid, body.get("phrase"), body.get("stage") or None,
                body.get("tag") or None, bool(body.get("notify", True)))
            return _json_resp({"ok": True, "id": rid})
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("uch_intent_rule_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_intent_rule_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rule_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор правила", 400)
        from services import intent_sensor
        ok = await intent_sensor.delete_rule(pool, uid, rid)
        return _json_resp({"ok": ok})

    async def uch_intent_rule_update(request: web.Request) -> web.Response:
        """Правка правила на месте: раньше опечатку во фразе чинили удалением и
        пересозданием — вместе со счётчиком срабатываний и журналом, то есть
        теряя ровно ту историю, по которой правило и настраивают."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rule_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        from services import intent_sensor
        kwargs = {}
        if "phrase" in body:
            kwargs["phrase"] = body.get("phrase")
        # stage/tag: None здесь — осмысленное «снять», поэтому различаем
        # «поле не прислали» и «прислали пустое».
        if "stage" in body:
            kwargs["stage"] = body.get("stage") or None
        if "tag" in body:
            kwargs["tag"] = body.get("tag") or None
        if "notify" in body:
            kwargs["notify"] = bool(body.get("notify"))
        try:
            ok = await intent_sensor.update_rule(pool, uid, rid, **kwargs)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("uch_intent_rule_update uid=%s rule=%s", uid, rid)
            return _err(_INTERNAL_ERROR, 500)
        if not ok:
            return _err("Правило не найдено", 404)
        return _json_resp({"ok": True})

    async def uch_intent_hits(request: web.Request) -> web.Response:
        """Последние срабатывания правил — чтобы их можно было отлаживать."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rid = None
        if request.query.get("rule_id"):
            try:
                rid = int(request.query["rule_id"])
            except (TypeError, ValueError):
                return _err("Неверный идентификатор правила", 400)
        from services import intent_sensor
        try:
            hits = await intent_sensor.recent_hits(pool, uid, rid, limit=50)
        except Exception:
            log.exception("uch_intent_hits uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"hits": hits})

    async def uch_intent_rule_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rule_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор правила", 400)
        from services import intent_sensor
        await intent_sensor.toggle_rule(pool, uid, rid)
        return _json_resp({"ok": True})

    async def uch_intent_seed(request: web.Request) -> web.Response:
        """Включить рекомендованный набор правил в один тап."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import intent_sensor
        added = await intent_sensor.seed_default_rules(pool, uid)
        return _json_resp({"ok": True, "added": added})

    async def uch_contact_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.repository import (
                get_contact, update_contact, log_contact_history)
            from services.contacts_hub.versioning_engine import (
                changed_against, create_version, snapshot_of, summary_of)
            # Снимок ДО правки. Экран «Версии» обещает «версии создаются при
            # изменениях», а создавал их НИКТО: create_version не звался ни
            # отсюда, ни откуда-либо ещё в продукте, так что список версий был
            # пуст всегда, а откату было некуда откатываться.
            before = await get_contact(pool, contact_id, uid)
            success = await update_contact(pool, contact_id, uid, data)
            if not success: return _err("Не найдено", 404)
            await log_contact_history(pool, contact_id, uid, 'edit', source='user')
            if before and before.get('contact'):
                snapshot = snapshot_of(before['contact'])
                # Только то, что правка реально меняет: экран отправляет
                # карточку целиком, и версия, подписанная «всем, что пришло»,
                # утверждала бы, что изменились телефоны, которых владелец не
                # касался. Ничего не изменилось — версии и не нужно.
                changed = changed_against(snapshot, data)
                try:
                    if changed:
                        await create_version(
                            pool, contact_id, uid, snapshot,
                            changed_fields=changed, changed_by='user',
                            change_summary=summary_of(changed))
                except Exception:
                    # Правка уже применена и подтверждена истории: ронять ответ
                    # из-за непишущейся версии значит показать владельцу ошибку
                    # там, где всё получилось.
                    log.exception("uch_contact_update: версия не создана")
            return _json_resp({'ok': True})
        except BadDate as bad:
            # Непонятная дата — это ошибка ввода, а не сбой сервера: 500 с
            # «внутренней ошибкой» заставлял владельца жать «Сохранить» снова.
            return _err(str(bad), 400)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_contact_update")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            from services.contacts_hub.repository import delete_contact
            success = await delete_contact(pool, contact_id, uid)
            if not success: return _err("Не найдено", 404)
            return _json_resp({'ok': True})
        except Exception:
            log.exception("uch_contact_delete")
            return _err(_INTERNAL_ERROR, 500)

    # ОТБОР СПИСКА — один, в фильтре списка. Здесь лежала ручка
    # GET /uch/search: те же семь колонок и теги, что у фильтра списка, но
    # своим условием и без пагинации, и её не звал ни один экран. Операторы
    # (@ник, #тег, company:, phone:), которые раньше знал только поиск бота,
    # теперь разбираются в repository._segment_where, поэтому работают в
    # списке, в срезе для массовых действий и в выгрузке — одним кодом.
    # Второй экран поиска НЕ заводить: он вернул бы расхождение «вижу одно —
    # применяю к другому», закрытое в выгрузке.

    async def uch_spotlight(request: web.Request) -> web.Response:
        """Подсказки по мере набора: ПРЫЖОК к контакту, а не отбор списка.

        Это не дубль фильтра: search_contacts_spotlight прощает опечатку
        (fuzzy-близость по имени/нику/компании) и ранжирует попадания — больше
        такого в продукте нет нигде. Ручка висела без двери, то есть
        устойчивость к опечаткам была написана и недоступна.

        Расхождения «вижу одно — применяю к другому» здесь не возникает:
        подсказка открывает ОДНУ карточку, а не задаёт срез, к которому
        применяются массовые действия.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        q = request.query.get('q', '').strip()
        if not q: return _json_resp({'results': []})
        try:
            from services.contacts_hub.search_engine import search_contacts_spotlight
            results = await search_contacts_spotlight(pool, uid, q, limit=8)
            out = [{'id': str(r.get('id')),
                    'name': (' '.join(x for x in (r.get('first_name'),
                                                  r.get('last_name')) if x)
                             or r.get('display_name') or r.get('username')
                             or 'Без имени'),
                    'username': r.get('username'),
                    'company': r.get('company'),
                    'tags': list(r.get('tags') or [])[:3]}
                   for r in results]
            return _json_resp({'results': out, 'total': len(out)})
        except Exception:
            log.exception("uch_spotlight")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.stats_engine import get_full_stats, get_account_stats
            stats = await get_full_stats(pool, uid)
            account_stats = await get_account_stats(pool, uid)
            return _json_resp({**stats, 'account_stats': account_stats})
        except Exception:
            log.exception("uch_stats")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_sync(request: web.Request) -> web.Response:
        """Запустить синхронизацию контактов флота в ФОНЕ (operation_bus).

        Раньше выполнялось инлайн в этом запросе: для 20+ аккаунтов Telethon-
        подключения не укладывались в таймаут шлюза, клиент отключался, aiohttp
        отменял обработчик на середине — в хаб попадали лишь часть контактов или
        ноль. Теперь ставим операцию и сразу отвечаем; op_worker обрабатывает без
        таймаута, а хаб подтягивает контакты по мере готовности.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            n = await _safe_count(pool,
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 "
                "AND session_str IS NOT NULL AND session_str <> ''", uid)
            if not n:
                return _err("Нет аккаунтов с сохранённой сессией. Подключите аккаунт "
                            "в разделе «Аккаунты», затем синхронизируйте контакты.", 400)
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "contacts_sync", {},
                total_items=int(n), label=f"Синхронизация контактов: {int(n)} акк.")
            return _json_resp({"ok": True, "queued": True, "op_id": op_id, "accounts": int(n)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("uch_sync submit uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_sync_center(request: web.Request) -> web.Response:
        """Центр синхронизации: что принёс каждый аккаунт и почему не принёс.

        Экран показывал «Всего контактов: N», дату последней синхронизации и
        плоский список «Аккаунт #12 — 340 контактов». Список строился по
        contact_sources, то есть аккаунт, у которого синхронизация ПАДАЕТ,
        в нём просто отсутствовал: сессия протухла полгода назад, контакты
        с него не идут, и узнать об этом было неоткуда.

        При этом причина пишется с самого начала: sync_account кладёт в
        contact_sync_log понятный русский текст (classify_session_error), а не
        сырой Telethon. Отдаём по аккаунту: сколько контактов собрано, когда
        была последняя попытка и чем кончилась.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            accs = await _safe_fetch(pool,
                """SELECT a.id, a.first_name, a.phone, a.acc_status,
                          (a.session_str IS NOT NULL AND a.session_str <> '') AS has_session,
                          COALESCE(c.cnt, 0) AS contact_count,
                          l.finished_at, l.contacts_synced, l.contacts_created,
                          l.error_message
                   FROM tg_accounts a
                   LEFT JOIN (
                       SELECT cs.account_id, COUNT(DISTINCT cs.contact_id) AS cnt
                       FROM contact_sources cs
                       JOIN unified_contacts uc ON uc.id = cs.contact_id
                       WHERE uc.owner_id = $1
                       GROUP BY cs.account_id
                   ) c ON c.account_id = a.id
                   LEFT JOIN LATERAL (
                       SELECT finished_at, contacts_synced, contacts_created, error_message
                       FROM contact_sync_log
                       WHERE owner_id = $1 AND account_id = a.id
                       ORDER BY COALESCE(finished_at, started_at) DESC
                       LIMIT 1
                   ) l ON TRUE
                   WHERE a.owner_id = $1
                   ORDER BY COALESCE(c.cnt, 0) DESC, a.id""", uid)
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", uid)
            last = await _safe_fetchrow(pool,
                "SELECT MAX(COALESCE(finished_at, started_at)) AS ts "
                "FROM contact_sync_log WHERE owner_id=$1", uid)
            running = await _safe_fetchrow(pool,
                "SELECT id, done_items, total_items FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='contacts_sync' "
                "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1", uid)
            items = []
            for a in accs:
                items.append({
                    "account_id": a["id"],
                    "name": a["first_name"] or "",
                    "phone": a["phone"] or "",
                    "has_session": bool(a["has_session"]),
                    "acc_status": a["acc_status"] or "",
                    "contact_count": int(a["contact_count"] or 0),
                    "last_sync": a["finished_at"].isoformat() if a["finished_at"] else None,
                    "last_synced": int(a["contacts_synced"] or 0),
                    "last_created": int(a["contacts_created"] or 0),
                    "error": a["error_message"] or "",
                })
            return _json_resp({
                "total": total,
                "last_sync": last["ts"].isoformat() if last and last["ts"] else None,
                "running": ({"op_id": running["id"],
                             "done": int(running["done_items"] or 0),
                             "total": int(running["total_items"] or 0)}
                            if running else None),
                "accounts": items,
                "ready": sum(1 for i in items if i["has_session"]),
                "failed": sum(1 for i in items if i["error"]),
            })
        except Exception:
            log.exception("uch_sync_center uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_groups(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.repository import get_contact_groups
            groups = await get_contact_groups(pool, uid)
            return _json_resp({'groups': groups})
        except Exception:
            log.exception("uch_groups")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_group_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import create_group
            gid = await create_group(pool, uid, data.get('name', ''), data.get('color'))
            return _json_resp({'ok': True, 'id': gid})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_group_create")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_group_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            gid = int(request.match_info['group_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор группы", 400)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import update_group
            ok = await update_group(pool, gid, uid, data.get('name'), data.get('color'))
            return _json_resp({'ok': ok})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_group_update")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_group_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            gid = int(request.match_info['group_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор группы", 400)
        try:
            from services.contacts_hub.bulk_ops_engine import delete_group
            ok = await delete_group(pool, gid, uid)
            return _json_resp({'ok': ok})
        except Exception:
            log.exception("uch_group_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_toggle_favorite(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.repository import update_contact
            row = await pool.fetchrow('SELECT is_favorite FROM unified_contacts WHERE id=$1 AND owner_id=$2', cid, uid)
            if not row: return _err("Не найдено", 404)
            new_val = not row['is_favorite']
            await update_contact(pool, cid, uid, {'is_favorite': new_val})
            return _json_resp({'ok': True, 'is_favorite': new_val})
        except Exception:
            log.exception("uch_toggle_favorite")
            return _err(_INTERNAL_ERROR, 500)

    # СПИСОК ПРАВОК КОНТАКТА отдельной ручкой больше не отдаётся: ручка
    # GET /uch/contacts/{id}/history была строгим подмножеством ленты
    # /timeline (та читает тот же contact_history и склеивает его с
    # источниками, событиями организма и стадией CRM), и её не звал ни один
    # экран. Две ленты истории расходились бы по скоупу: /timeline проверяет
    # владельца через _own_contact, а здесь владелец был только в WHERE.

    async def uch_contact_invite_history(request: web.Request) -> web.Response:
        """Кому/куда/когда пытались пригласить этот контакт (по всем группам).

        Закрывает изоляцию invite_target_log от CRM: раньше эта информация
        технически была в БД, но негде было её увидеть рядом с контактом."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contact_invite_link import invite_history_for_contact, is_contact_opted_out
            history = await invite_history_for_contact(pool, uid, cid)
            opted_out = await is_contact_opted_out(pool, uid, cid)
            return _json_resp({'history': history, 'opted_out': opted_out})
        except Exception:
            log.exception("uch_contact_invite_history")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_opt_out(request: web.Request) -> web.Response:
        """Пометить контакт «не приглашать» — сразу по всем его формам
        (id/username/телефоны), не только по одной (см. contact_invite_link.py:
        opt-out по одной форме давал ложное чувство защиты)."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            body = await request.json() if request.can_read_body else {}
            reason = validate_string(body.get('reason'), max_len=300) if body.get('reason') else None
            from services.contact_invite_link import opt_out_contact
            stored = await opt_out_contact(pool, uid, cid, reason=reason)
            return _json_resp({'ok': bool(stored), 'targets': stored})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_contact_opt_out")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_allow_invite(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contact_invite_link import allow_contact_invite
            removed = await allow_contact_invite(pool, uid, cid)
            return _json_resp({'ok': removed})
        except Exception:
            log.exception("uch_contact_allow_invite")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_contact_versions(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.versioning_engine import get_versions
            versions = await get_versions(pool, cid, uid)
            return _json_resp({'versions': versions})
        except Exception:
            log.exception("uch_contact_versions")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_rollback_preview(request: web.Request) -> web.Response:
        """Что именно затрёт откат — ДО того, как владелец его подтвердит.

        Экран спрашивал «Откатить к версии 3?» и больше ничего: необратимая
        перезапись карточки предлагалась к подписи вслепую.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            vnum = int(request.match_info['version_num'])
            from services.contacts_hub.versioning_engine import rollback_preview
            prev = await rollback_preview(pool, cid, uid, vnum)
            if prev is None: return _err("Версия не найдена", 404)
            return _json_resp(prev)
        except ValueError:
            return _err("Неверный номер версии", 400)
        except Exception:
            log.exception("uch_rollback_preview")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_rollback(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            vnum = int(request.match_info['version_num'])
        except (KeyError, ValueError):
            return _err("Неверный номер версии", 400)
        try:
            from services.contacts_hub.versioning_engine import rollback_to_version
            ok = await rollback_to_version(pool, cid, uid, vnum)
            if not ok: return _err("Версия не найдена", 404)
            return _json_resp({'ok': True})
        except Exception:
            log.exception("uch_rollback")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_timeline(request: web.Request) -> web.Response:
        """Карточка-360: единая лента касаний контакта из всех подсистем —
        правки полей, источники, CRM-стадия и события организма (intent с этим
        contact_id) через чистый contacts_hub.timeline.build_timeline."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            if not await _own_contact(cid, uid):
                return _err("Контакт не найден", 404)
            history = await pool.fetch(
                'SELECT field_name, old_value, new_value, created_at FROM contact_history WHERE contact_id=$1 AND owner_id=$2 ORDER BY created_at DESC LIMIT 60',
                cid, uid)
            srcs = await _safe_fetch(pool,
                'SELECT account_id, first_seen_at FROM contact_sources WHERE contact_id=$1', cid)
            events = await _safe_fetch(pool,
                "SELECT kind, payload, created_at FROM organism_events "
                "WHERE owner_id=$1 AND payload->>'contact_id'=$2 ORDER BY created_at DESC LIMIT 60",
                uid, str(cid))
            crm_row = await _safe_fetchrow(pool,
                'SELECT stage, updated_at FROM contact_crm WHERE contact_id=$1', cid)
            from services.contacts_hub import timeline as _tl
            def _payload(p):
                if isinstance(p, dict):
                    return p
                try:
                    return _json.loads(p) if p else {}
                except Exception:
                    return {}
            items = _tl.build_timeline(
                history=[{"field": r["field_name"], "old_value": r["old_value"],
                          "new_value": r["new_value"], "created_at": r["created_at"]}
                         for r in (history or [])],
                events=[{"kind": r["kind"], "payload": _payload(r["payload"]),
                         "created_at": r["created_at"]} for r in (events or [])],
                sources=[{"account_id": r["account_id"], "discovered_at": r["first_seen_at"]}
                         for r in (srcs or [])],
                crm=dict(crm_row) if crm_row else None)
            out = [{**i, "ts": i["ts"].isoformat() if hasattr(i["ts"], "isoformat") else i["ts"]}
                   for i in items]
            # Лента склеена из разных источников, поэтому «сколько всего» для
            # неё не существует как одно число. Но сказать, что показано не
            # всё, обязаны: каждая часть взята с потолком, и при его достижении
            # более ранние касания в ленту просто не попали.
            truncated = len(history or []) >= 60 or len(events or []) >= 60
            return _json_resp({'timeline': out, 'count': len(out),
                               'truncated': truncated})
        except Exception:
            log.exception("uch_timeline")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_relationships(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.relationship_engine import get_relationships
            rels = await get_relationships(pool, uid, cid)
            return _json_resp({'relationships': rels})
        except Exception:
            log.exception("uch_relationships")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_identity(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            if not await _own_contact(cid, uid):
                return _err("Контакт не найден", 404)
            from services.contacts_hub.identity_engine import build_identity_graph, get_last_active
            graph = await build_identity_graph(pool, uid, cid)
            last_active = await get_last_active(pool, cid, uid)
            return _json_resp({**graph, 'last_active': last_active})
        except Exception:
            log.exception("uch_identity")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_crm_pipeline(request: web.Request) -> web.Response:
        """Воронка хаба: сколько контактов в каждой стадии и на какую сумму."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.crm_engine import pipeline
            return _json_resp(await pipeline(pool, uid))
        except Exception:
            log.exception("uch_crm_pipeline")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_crm_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.crm_engine import get_crm_data, get_crm_activity
            crm = await get_crm_data(pool, uid, cid)
            activity = await get_crm_activity(pool, uid, cid)
            return _json_resp({'crm': crm, 'activity': activity})
        except Exception:
            log.exception("uch_crm_get")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_crm_upsert(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.crm_engine import ContactNotFound, upsert_crm
            crm = await upsert_crm(pool, uid, cid, data)
            return _json_resp({'ok': True, 'crm': crm})
        except ContactNotFound as nf:
            return _err(str(nf), 404)
        except ValueError as ve:
            # BadStage — подвид ValueError: «Неизвестная стадия сделки» уходит
            # владельцу как есть, по-русски.
            return _err(str(ve), 400)
        except Exception:
            log.exception("uch_crm_upsert uid=%s", uid)
            return _err("Не удалось сохранить CRM", 500)

    async def uch_crm_activity(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.crm_engine import (
                ACTIVITY_LABELS, ContactNotFound, activity_label,
                log_crm_activity,
            )
            kind = str(data.get('type') or 'note')
            # Тип приходит с экрана и ложится в базу. Свободная строка
            # превратила бы журнал в свалку, по которой нельзя ни считать, ни
            # группировать, поэтому список закрытый — и отказ по-русски.
            if kind not in ACTIVITY_LABELS:
                return _err("Неизвестный тип касания", 400)
            desc = data.get('description')
            if desc is not None:
                desc = validate_string(desc, max_len=500)
            await log_crm_activity(pool, uid, cid, kind, desc,
                                   data.get('metadata'))
            return _json_resp({'ok': True, 'label': activity_label(kind)})
        except ContactNotFound as nf:
            return _err(str(nf), 404)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_crm_activity")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_crm_reminder(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            # next_reminder_at — TIMESTAMPTZ, а asyncpg биндит его БЕЗ каста и
            # требует datetime: строка с фронта роняла запрос (DataError) → фича
            # «напоминание» падала 500 всегда. Парсим на границе; naive-время
            # (старые клиенты) трактуем как UTC, чтобы не сдвигать.
            import datetime as _dtm
            _raw = data.get('remind_at')
            remind_dt = None
            if _raw:
                try:
                    remind_dt = _dtm.datetime.fromisoformat(
                        str(_raw).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    return _err("Некорректная дата напоминания", 400)
                if remind_dt.tzinfo is None:
                    remind_dt = remind_dt.replace(tzinfo=_dtm.timezone.utc)
            from services.contacts_hub.crm_engine import ContactNotFound, upsert_crm
            await upsert_crm(pool, uid, cid, {
                'next_reminder_at': remind_dt,
                'next_reminder_text': data.get('text', ''),
            })
            return _json_resp({'ok': True})
        except ContactNotFound as nf:
            return _err(str(nf), 404)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_crm_reminder uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_duplicates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import detect_smart_duplicates
            res = await detect_smart_duplicates(pool, uid)
            dupes = res['duplicates']
            # Сколько групп не сравнивалось — часть ответа, а не деталь
            # реализации: молча срезанный список читается как «дублей больше
            # нет», и владелец закрывает экран с этим выводом.
            return _json_resp({'duplicates': dupes, 'count': len(dupes),
                               'skipped_groups': res['skipped_groups']})
        except Exception:
            log.exception("uch_duplicates")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_merge_preview(request: web.Request) -> web.Response:
        """Что переедет при слиянии — ДО подтверждения.

        Тот же путь, что и само слияние: GET — что будет, POST — сделать.
        Считает теми же правилами, что и слияние, поэтому разойтись с ним
        не может.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            primary_id = (request.query.get('primary_id') or '').strip()
            secondary_id = (request.query.get('secondary_id') or '').strip()
            if not primary_id or not secondary_id:
                return _err("Нужны основной и дополнительный идентификаторы", 400)
            from services.contacts_hub.merge_engine import merge_preview
            res = await merge_preview(pool, primary_id, secondary_id, uid)
            if 'error' in res:
                return _err("Контакт не найден", 404)
            return _json_resp(res)
        except Exception:
            log.exception("uch_merge_preview")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_merge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            primary_id = data.get('primary_id')
            secondary_id = data.get('secondary_id')
            if not primary_id or not secondary_id:
                return _err("Нужны основной и дополнительный идентификаторы", 400)
            from services.contacts_hub.merge_engine import manual_merge
            result = await manual_merge(pool, primary_id, secondary_id, uid)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_merge")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_conflicts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import get_conflicts
            conflicts = await get_conflicts(pool, uid)
            return _json_resp({'conflicts': conflicts})
        except Exception:
            log.exception("uch_conflicts")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_resolve_conflict(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info['conflict_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор конфликта", 400)
        try:
            data = await request.json()
            from services.contacts_hub.trust_engine import resolve_conflict
            ok = await resolve_conflict(pool, cid, uid, data.get('resolution', 'accepted'),
                                        data.get('value'), uid)
            return _json_resp({'ok': ok})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_resolve_conflict")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tags(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import get_smart_tags
            tags = await get_smart_tags(pool, uid)
            return _json_resp({'tags': tags})
        except Exception:
            log.exception("uch_smart_tags")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tag_rules(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import get_smart_tag_rules
            rules = await get_smart_tag_rules(pool, uid)
            return _json_resp({'rules': rules})
        except Exception:
            log.exception("uch_smart_tag_rules")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tags_apply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import apply_smart_tags
            result = await apply_smart_tags(pool, uid)
            return _json_resp(result)
        except Exception:
            log.exception("uch_smart_tags_apply")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tag_rule_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.smart_tags_engine import create_smart_tag_rule
            rid = await create_smart_tag_rule(pool, uid, data.get('name', ''),
                                              data.get('tag', ''), data.get('conditions', {}))
            return _json_resp({'ok': True, 'id': rid})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_smart_tag_rule_create")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tag_rule_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info['rule_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор правила", 400)
        try:
            from services.contacts_hub.smart_tags_engine import delete_smart_tag_rule
            ok = await delete_smart_tag_rule(pool, rid, uid)
            return _json_resp({'ok': ok})
        except Exception:
            log.exception("uch_smart_tag_rule_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_smart_tag_rule_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info['rule_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор правила", 400)
        try:
            from services.contacts_hub.smart_tags_engine import toggle_smart_tag_rule
            active = await toggle_smart_tag_rule(pool, rid, uid)
            return _json_resp({'ok': True, 'is_active': active})
        except Exception:
            log.exception("uch_smart_tag_rule_toggle")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_tag(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_tag
            result = await bulk_tag(pool, uid, data.get('contact_ids', []), data.get('tag', ''))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_tag")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_untag(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_untag
            result = await bulk_untag(pool, uid, data.get('contact_ids', []), data.get('tag', ''))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_untag")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_favorite(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_set_favorite
            result = await bulk_set_favorite(pool, uid, data.get('contact_ids', []), data.get('is_favorite', True))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_favorite")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_delete
            result = await bulk_delete(pool, uid, data.get('contact_ids', []))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_exclude(request: web.Request) -> web.Response:
        """Пометить/снять «личный» (исключить из рабочих сегментов) по списку id."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import set_excluded
            result = await set_excluded(pool, uid, data.get('contact_ids', []),
                                        bool(data.get('excluded', True)))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_exclude")
            return _err(_INTERNAL_ERROR, 500)

    def _filter_account_id(body: dict):
        """id аккаунта из фильтра: None (не задан), число или False (мусор).

        Раньше `int(body['account_id'])` стоял прямо в вызове внутри try, и
        нечисловое значение выпадало пятисоткой — «внутренняя ошибка сервера»
        вместо «неверный идентификатор аккаунта».
        """
        raw = body.get('account_id')
        if not raw:
            return None
        return validate_integer(raw, min_val=1) or False

    async def uch_filter_preview(request: web.Request) -> web.Response:
        """Сколько контактов попадёт под фильтр (страна/тег/источник) — до действия."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            d = await request.json()
            acc_id = _filter_account_id(d)
            if acc_id is False:
                return _err("Неверный идентификатор аккаунта", 400)
            from services.contacts_hub.bulk_ops_engine import count_by_filter
            n = await count_by_filter(
                pool, uid, country=(d.get('country') or None),
                tag=(d.get('tag') or None), account_id=acc_id)
            return _json_resp({'count': n})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_filter_preview")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_filter_exclude(request: web.Request) -> web.Response:
        """Пометить «личными»/снять пометку у всех под фильтр (страна/тег/источник)."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            d = await request.json()
            acc_id = _filter_account_id(d)
            if acc_id is False:
                return _err("Неверный идентификатор аккаунта", 400)
            from services.contacts_hub.bulk_ops_engine import exclude_by_filter
            result = await exclude_by_filter(
                pool, uid, excluded=bool(d.get('excluded', True)),
                country=(d.get('country') or None), tag=(d.get('tag') or None),
                account_id=acc_id)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_filter_exclude")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_filter_delete(request: web.Request) -> web.Response:
        """Массовое удаление контактов под фильтр (страна/тег/источник). Требует
        confirm=true — защита от случайного сноса."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            d = await request.json()
            if not d.get('confirm'):
                return _err("Нужно подтверждение удаления", 400)
            if not any([d.get('country'), d.get('tag'), d.get('account_id')]):
                return _err("пустой фильтр — нечего удалять", 400)
            from services.contacts_hub.bulk_ops_engine import delete_by_filter
            result = await delete_by_filter(
                pool, uid, country=(d.get('country') or None),
                tag=(d.get('tag') or None),
                account_id=(int(d['account_id']) if d.get('account_id') else None))
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_filter_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_group(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            gid = data.get('group_id')
            if not gid: return _err("Нужен идентификатор группы", 400)
            action = data.get('action', 'add')
            if action == 'add':
                from services.contacts_hub.bulk_ops_engine import bulk_add_to_group
                result = await bulk_add_to_group(pool, uid, data.get('contact_ids', []), gid)
            else:
                from services.contacts_hub.bulk_ops_engine import bulk_remove_from_group
                result = await bulk_remove_from_group(pool, uid, data.get('contact_ids', []), gid)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_group")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_merge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            pairs = data.get('pairs', [])
            if not pairs: return _err("Нужны пары значений", 400)
            from services.contacts_hub.bulk_ops_engine import bulk_merge
            result = await bulk_merge(pool, uid, pairs)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_merge")
            return _err(_INTERNAL_ERROR, 500)

    # Ручка POST /uch/bulk/export удалена: выгрузка выбранных идёт тем же
    # GET /uch/export/{fmt}?ids=…, что и весь хаб и текущий срез. Она была
    # второй реализацией той же выгрузки, и её не звал ни один экран —
    # «выгрузить отмеченных» в интерфейсе просто не было.
    async def uch_bulk_importance(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            contact_ids = data.get('contact_ids', [])
            level = data.get('level', 0)
            from services.contacts_hub.bulk_ops_engine import bulk_set_importance
            result = await bulk_set_importance(pool, uid, contact_ids, level)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_importance")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_bulk_rating(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            contact_ids = data.get('contact_ids', [])
            rating = data.get('rating', 0)
            from services.contacts_hub.bulk_ops_engine import bulk_set_rating
            result = await bulk_set_rating(pool, uid, contact_ids, rating)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_bulk_rating")
            return _err(_INTERNAL_ERROR, 500)

    _EXPORT_FORMATS = {
        'csv': ('text/csv', 'contacts.csv'),
        'vcf': ('text/vcard', 'contacts.vcf'),
        'json': ('application/json', 'contacts.json'),
    }
    # Сколько контактов выгружаем срезом за один раз. Выгрузка молча обрезанного
    # среза — это файл, в котором чего-то нет, и владелец об этом не узнает;
    # поэтому на превышении приходит отказ с числом, а не усечённый файл.
    _EXPORT_SLICE_CAP = 5000

    async def _export_target_ids(request: web.Request, uid: int):
        """Кого выгружаем: (ids | None, ошибка | None).

        None означает «весь хаб» — прежнее и единственное поведение экспорта.
        Список id приходит либо прямо (`ids=` — выбранные галочками, и так же
        выгружается один контакт из карточки), либо считается по ТЕМ ЖЕ
        фильтрам, что у списка (`_contact_filters`), то есть «выгрузить то, что
        вижу».
        """
        raw = (request.query.get('ids') or '').strip()
        if raw:
            ids = [x for x in (s.strip() for s in raw.split(',')) if x]
            if len(ids) > _EXPORT_SLICE_CAP:
                return None, f"За раз выгружается не больше {_EXPORT_SLICE_CAP} контактов"
            for x in ids:
                # id контакта — UUID; что угодно другое уронило бы запрос
                # целиком (asyncpg биндит uuid без каста).
                if not _UUID_RE.match(x):
                    return None, "Неверный идентификатор контакта"
            return ids, None
        filters = _contact_filters(request)
        if not _filters_are_set(filters):
            return None, None
        from services.contacts_hub.repository import get_contacts
        res = await get_contacts(pool, uid, limit=_EXPORT_SLICE_CAP, offset=0,
                                 **filters)
        rows = res.get('contacts') or []
        total = int(res.get('total') or len(rows))
        if total > _EXPORT_SLICE_CAP:
            return None, (
                f"В срезе {total} контактов, за раз выгружается "
                f"{_EXPORT_SLICE_CAP}. Сузьте фильтр или выгрузите весь хаб.")
        return [str(r['id']) for r in rows], None

    async def uch_export(request: web.Request) -> web.Response:
        """Экспорт контактов: весь хаб, текущий срез или выбранные.

        Раньше здесь лежали три почти одинаковых обработчика, и каждый умел
        ровно одно — «все контакты». Фильтр хаба (стадия, тег, группа,
        аккаунт, пол, личные) на экспорт не влиял никак, то есть владелец,
        собравший срез, выгружал вместо него весь справочник.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        fmt = (request.match_info.get('fmt') or 'csv').lower()
        if fmt not in _EXPORT_FORMATS:
            return _err("Неизвестный формат выгрузки", 400)
        content_type, filename = _EXPORT_FORMATS[fmt]
        try:
            ids, err = await _export_target_ids(request, uid)
            if err:
                return _err(err, 400)
            if ids is not None and not ids:
                return _err("В этом срезе нет контактов", 400)
            from services.contacts_hub import export_engine as _ex
            fn = {'csv': _ex.export_csv, 'vcf': _ex.export_vcf,
                  'json': _ex.export_json}[fmt]
            data = await fn(pool, uid, ids)
            return web.Response(
                body=data, content_type=content_type,
                headers={'Content-Disposition': f'attachment; filename="{filename}"'})
        except Exception:
            log.exception("uch_export fmt=%s", fmt)
            return _err(_INTERNAL_ERROR, 500)

    async def uch_reminders(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.crm_engine import get_upcoming_reminders, get_crm_overdue
            upcoming = await get_upcoming_reminders(pool, uid)
            overdue = await get_crm_overdue(pool, uid)
            return _json_resp({'upcoming': upcoming, 'overdue': overdue})
        except Exception:
            log.exception("uch_reminders")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_graph_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.relationship_engine import get_graph_stats
            stats = await get_graph_stats(pool, uid)
            return _json_resp(stats)
        except Exception:
            log.exception("uch_graph_stats")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_graph_pairs(request: web.Request) -> web.Response:
        """Пары одного типа связи — чтобы счётчик «По типам» открывался списком."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        rel_type = validate_string(request.query.get("type"), max_len=64)
        if not rel_type:
            return _err("Не указан тип связи", 400)
        limit = _list_limit(request, default=50, maximum=200)
        try:
            from services.contacts_hub.relationship_engine import (
                RELATIONSHIP_TYPES, count_pairs_by_type, get_pairs_by_type)
            pairs = await get_pairs_by_type(pool, uid, rel_type, limit)
            # Предыдущий экран показал число связей этого типа. Если список
            # обрезан потолком, экран обязан сказать это сам, иначе число и
            # список противоречат друг другу молча.
            total = await count_pairs_by_type(pool, uid, rel_type)
            return _json_resp({
                'type': rel_type,
                'type_label': RELATIONSHIP_TYPES.get(rel_type, rel_type),
                'pairs': pairs,
                'total': total,
                'limit': limit,
            })
        except Exception:
            log.exception("uch_graph_pairs")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_graph_compute(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.relationship_engine import compute_relationships
            result = await compute_relationships(pool, uid)
            return _json_resp(result)
        except Exception:
            log.exception("uch_graph_compute")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_trust_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import update_trust_scores
            updated = await update_trust_scores(pool, uid)
            return _json_resp({'updated': updated})
        except Exception:
            log.exception("uch_trust_update")
            return _err(_INTERNAL_ERROR, 500)

    async def uch_ai_query(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            query = data.get('query', '').strip()
            if not query: return _err("Нужен поисковый запрос", 400)
            from services.contacts_hub.ai_assistant import process_ai_query
            result = await process_ai_query(pool, uid, query)
            return _json_resp(result)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("uch_ai_query")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/uch/contacts", uch_contacts)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}", uch_contact_detail)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}", uch_contact_update)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/message", uch_contact_message)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/media", uch_contact_media)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/invite", uch_contact_invite)
    app.router.add_post("/api/miniapp/uch/segment/preview", uch_segment_preview)
    app.router.add_post("/api/miniapp/uch/segment/message", uch_segment_message)
    app.router.add_post("/api/miniapp/uch/segment/media", uch_segment_media)
    app.router.add_post("/api/miniapp/uch/segment/invite", uch_segment_invite)
    app.router.add_get("/api/miniapp/uch/segments", uch_segments_list)
    app.router.add_post("/api/miniapp/uch/segments/save", uch_segment_save)
    app.router.add_delete("/api/miniapp/uch/segments/{seg_id}", uch_segment_delete)
    app.router.add_get("/api/miniapp/uch/intent/rules", uch_intent_rules)
    app.router.add_post("/api/miniapp/uch/intent/rule", uch_intent_rule_create)
    app.router.add_delete("/api/miniapp/uch/intent/rule/{rule_id}", uch_intent_rule_delete)
    app.router.add_post("/api/miniapp/uch/intent/rule/{rule_id}/toggle", uch_intent_rule_toggle)
    app.router.add_patch("/api/miniapp/uch/intent/rule/{rule_id}", uch_intent_rule_update)
    app.router.add_get("/api/miniapp/uch/intent/hits", uch_intent_hits)
    app.router.add_post("/api/miniapp/uch/intent/seed", uch_intent_seed)
    app.router.add_delete("/api/miniapp/uch/contacts/{contact_id}", uch_contact_delete)
    app.router.add_get("/api/miniapp/uch/spotlight", uch_spotlight)
    app.router.add_get("/api/miniapp/uch/stats", uch_stats)
    app.router.add_post("/api/miniapp/uch/sync", uch_sync)
    app.router.add_get("/api/miniapp/uch/sync_center", uch_sync_center)
    app.router.add_get("/api/miniapp/uch/groups", uch_groups)
    app.router.add_post("/api/miniapp/uch/groups", uch_group_create)
    app.router.add_put("/api/miniapp/uch/groups/{group_id}", uch_group_update)
    app.router.add_delete("/api/miniapp/uch/groups/{group_id}", uch_group_delete)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/favorite", uch_toggle_favorite)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/invite_history", uch_contact_invite_history)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/opt_out", uch_contact_opt_out)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/allow_invite", uch_contact_allow_invite)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/versions", uch_contact_versions)
    # Тот же путь: GET — что изменится, POST — изменить.
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/rollback/{version_num}", uch_rollback_preview)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/rollback/{version_num}", uch_rollback)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/timeline", uch_timeline)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/relationships", uch_relationships)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/identity", uch_identity)
    app.router.add_get("/api/miniapp/uch/crm/pipeline", uch_crm_pipeline)
    app.router.add_get("/api/miniapp/uch/export/{fmt}", uch_export)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/crm", uch_crm_get)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm", uch_crm_upsert)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm/activity", uch_crm_activity)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm/reminder", uch_crm_reminder)
    app.router.add_get("/api/miniapp/uch/duplicates", uch_duplicates)
    # Тот же путь: GET — что переедет, POST — слить.
    app.router.add_get("/api/miniapp/uch/merge", uch_merge_preview)
    app.router.add_post("/api/miniapp/uch/merge", uch_merge)
    app.router.add_get("/api/miniapp/uch/conflicts", uch_conflicts)
    app.router.add_post("/api/miniapp/uch/conflicts/{conflict_id}/resolve", uch_resolve_conflict)
    app.router.add_get("/api/miniapp/uch/smart-tags", uch_smart_tags)
    app.router.add_get("/api/miniapp/uch/smart-tags/rules", uch_smart_tag_rules)
    app.router.add_post("/api/miniapp/uch/smart-tags/apply", uch_smart_tags_apply)
    app.router.add_post("/api/miniapp/uch/smart-tags/rules", uch_smart_tag_rule_create)
    app.router.add_delete("/api/miniapp/uch/smart-tags/rules/{rule_id}", uch_smart_tag_rule_delete)
    app.router.add_put("/api/miniapp/uch/smart-tags/rules/{rule_id}/toggle", uch_smart_tag_rule_toggle)
    app.router.add_post("/api/miniapp/uch/bulk/tag", uch_bulk_tag)
    app.router.add_post("/api/miniapp/uch/bulk/untag", uch_bulk_untag)
    app.router.add_post("/api/miniapp/uch/bulk/favorite", uch_bulk_favorite)
    app.router.add_post("/api/miniapp/uch/bulk/delete", uch_bulk_delete)
    app.router.add_post("/api/miniapp/uch/bulk/exclude", uch_bulk_exclude)
    app.router.add_post("/api/miniapp/uch/filter/preview", uch_filter_preview)
    app.router.add_post("/api/miniapp/uch/filter/exclude", uch_filter_exclude)
    app.router.add_post("/api/miniapp/uch/filter/delete", uch_filter_delete)
    app.router.add_post("/api/miniapp/uch/bulk/group", uch_bulk_group)
    app.router.add_post("/api/miniapp/uch/bulk/merge", uch_bulk_merge)
    app.router.add_post("/api/miniapp/uch/bulk/importance", uch_bulk_importance)
    app.router.add_post("/api/miniapp/uch/bulk/rating", uch_bulk_rating)
    app.router.add_get("/api/miniapp/uch/reminders", uch_reminders)
    app.router.add_get("/api/miniapp/uch/graph/stats", uch_graph_stats)
    app.router.add_get("/api/miniapp/uch/graph/pairs", uch_graph_pairs)
    app.router.add_post("/api/miniapp/uch/graph/compute", uch_graph_compute)
    app.router.add_post("/api/miniapp/uch/trust/update", uch_trust_update)
    app.router.add_post("/api/miniapp/uch/ai", uch_ai_query)
