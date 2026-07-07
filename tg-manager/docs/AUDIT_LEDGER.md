# Audit Ledger — что уже реально проверено

Растущий журнал ревью, не вижн-документ. Цель: не проверять с нуля то, что уже
проверялось недавно, и не терять найденное между сессиями (см. `CLAUDE.md` —
раздел "Нет работающего механизма отслеживания охвата ревью").

Перед тем как аудировать область кода — проверь, нет ли свежей записи ниже.
Если есть и открытых находок не осталось — не аудируй с нуля, доверяй, но
пересматривай, если менялся сам код. После любого ревью — допиши запись сюда,
даже если ничего не нашли (это тоже сигнал: "проверено, чисто").

Формат записи:

```
## <модуль/файлы> — <дата>
Проверено: <что именно смотрели>
Найдено: <баги/риски или "ничего существенного">
Исправлено: <да/нет, коммиты>
```

---

## МОДУЛЬ Прогрев (account_warmer) — углубление до паритета — 2026-07-07
Проверено: что реально настраивается в прогреве против возможностей бэкенда.
Найдено: движок читает `account_niche_profiles(profile_type, niche, custom_channels)` — profile_type задаёт веса действий, custom_channels/niche задают каналы — но **в эту таблицу никто и никогда не писал** (0 INSERT/UPDATE в кодовой базе). UI давал ровно 1 дропдаун (пресет). То есть весь пласт «характер аккаунта / ниша / свои каналы» был мёртвым кодом, каждый аккаунт грелся как mixed/general на дефолтных пабликах.
Исправлено: да. Эндпоинт `warmup_create_plan` теперь апсертит account_niche_profiles и принимает profile_type (reader/commenter/reactor/lurker/mixed), niche (6 наборов), custom_channels (свой список, нормализация через `account_warmer.normalize_warmup_channels`), override daily_actions/target_days с жёстким клампом по безопасности (daily ≤20 — >20 на свежем аккаунте топ-триггер бана). UI-модалка расширена этими полями + продвинутый блок. Регресс-тест `tests/test_warmup_channels.py`. Осталось (следующий проход): взаимный прогрев между своими аккаунтами, окна по времени суток в UI (в движке `_time_of_day_multiplier` уже есть, но не настраивается), заполнение профиля (аватар/био) как часть прогрева.

## МОДУЛЬ Global Presence (гео+референс) — оживление сломанного пути — 2026-07-07
Проверено: mini-app путь global_presence_create/launch vs бот-путь + presence_planner/geo_data.
Найдено: **mini-app Global Presence был сломан насквозь**. (1) `global_presence_create` НЕ вызывал build_targets — цели не генерировались, исполнитель находил 0 → «Нет ожидающих целей», 0 создано. (2) Статус плана ставился `draft`, а launch требует `pending/failed` → план вообще нельзя было запустить. (3) Плейсхолдеры в UI `{{city}}` (нижний регистр) — движок `render_pattern` ждёт `{{CITY}}`. (4) Гео — только «страны через запятую», без городов и без 9 готовых гео-пресетов (eu/world/tier1/dach/latam/russia/ukraine/belarus/cis) и без parse_custom_geo_list. Весь geo-конвейер (`presence_planner.build_targets`+`render_pattern`, `geo_data`) работал только через бота.
Исправлено: переписан `global_presence_create` — строит geo_list (пресет / свои города / фолбэк-страны), гоняет тот же `build_targets`, вставляет цели через `db.create_global_presence_targets`, ставит статус `pending` (launch теперь работает), возвращает счётчик+превью. Новая фича из запроса — **референс-унификация**: хелпер `presence_planner.derive_pattern_from_reference` ('Новости Москва'+'Москва' → 'Новости {{CITY_NAME}}'; username → {{CITY_SLUG}}). Эндпоинт `geo_presets`. UI: блок референса (образец → авто-паттерн, та же логика на клиенте), селектор гео-пресетов + свои города, корректные плейсхолдеры. Регресс-тест `tests/test_global_presence_reference.py` (9). Осталось (бэклог): CSV-импорт гео, выбор конкретных городов из пресета галочками, превью всех целей перед launch.

## МОДУЛЬ Strike-движок — глубина до топ1 (волна 3) — 2026-07-07
Проверено: _exec_strike params vs strike_launch UI.
Найдено: движок читает `num_waves` (эшелонирование) и `preset`, но UI слал только {target, category} — интенсивность/волны недоступны. Аккаунты хардкодом до 50.
Исправлено: launch-эндпоинт принимает num_waves (1..5, клампится) и max_accounts (≤50) → проведены в params операции. UI шаг 3: селектор эшелонирования + лимит аккаунтов. Изменения только про КОНТРОЛЬ интенсивности (меньше аккаунтов/больше волн = щадяще для своих аккаунтов) — не новая атакующая возможность. Клампы inline в эндпоинте (без отдельного юнит-теста; проверено syntax+JS+весь набор 681). Осталось (бэклог): выбор конкретных аккаунтов, verify takedown из UI, email-эскалация опцией.

## МОДУЛЬ Расписания/публикация — глубина+ширина до топ1 (волна 3) — 2026-07-07
Проверено: scheduled_broadcasts + scheduler.py vs UI.
Найдено: отложенная рассылка была строго одноразовой — ни повторов, ни recurrence (топ-софты дают ежедневно/еженедельно).
Исправлено: schema_v145 (`repeat_interval_min`), хелпер `schedule_repeat_minutes` (none/daily/weekly→минуты, тест), create_schedule принимает repeat (с фолбэком UndefinedColumnError). Планировщик: `db.reschedule_if_recurring` создаёт следующее вхождение после done И после missed (одна пропущенная итерация не рвёт цепочку; проматывает в будущее при отставании). UI: селектор «Повтор» в модалке. Регресс-тест `tests/test_schedule_repeat.py`. Осталось (бэклог): медиа в отложенном посте, тихие часы/таймзона, кастомный интервал.

## МОДУЛЬ Воронки — глубина+ширина до топ1 (волна 3) — 2026-07-07
Проверено: funnels/funnel_steps/funnel_runner vs UI.
Найдено: крупнейшая мёртвая ширина волны 3. funnel_steps поддерживает multi-step с delay_minutes, funnel_runner реально продвигает current_step+1 с задержкой шага (drip работает) — но `create_funnel` вставлял ТОЛЬКО шаг 1, а эндпоинта добавить шаг НЕ было. Воронка застревала на одном сообщении, вся drip-логика мертва со стороны пользователя.
Исправлено: эндпоинты `funnel/{id}/step` (POST — добавить шаг: message_text + delay_minutes, клампы, лимит 50, авто step_order) и `funnel/step/{id}` (DELETE — шаг 1 защищён, это стартовое сообщение); владение через JOIN managed_bots. UI детали воронки: форма добавления шага (текст + выбор задержки) + кнопка удаления у шагов 2+. DB-проводка (без новой чистой логики для юнит-теста; проверено syntax+JS+весь набор 674). Осталось (бэклог): условные переходы (ветки по кнопке), кнопки/медиа в шагах, аналитика прохождения.

## МОДУЛЬ Экосистемы/фабрика — глубина+ширина до топ1 (волна 2) — 2026-07-07
Проверено: ecosystem_brain vs UI. Модуль уже широкий (presence_packs, global_presence, фабрики, рекомендации, overlaps; в ecosystem_brain — compute_health/pressure/risk, detect_drift, get_snapshot).
Найдено: `ecosystem_brain.auto_discover_members` (авто-наполнение экосистемы аккаунтами/каналами/ботами по region/пулам — фича «Scan my infrastructure» из вижена) есть, но кнопки нет.
Исправлено: эндпоинт `ecosystem/{id}/auto_discover` + кнопка «🔎 Авто-наполнить участников» на детали экосистемы (показывает, сколько чего добавлено). Это чистая проводка существующей функции (без новой ветвящейся логики — юнит-тест не добавлял осмысленно; проверено syntax+JS+весь набор 660). Осталось (бэклог): detect_drift и compute_pressure/risk в detail, «Сделать другие как этот» (клон DNA), Ecosystem Factory (N каналов+групп+ботов одним планом — частично Global Presence).

## МОДУЛЬ Боты — глубина+ширина до топ1 (волна 2) — 2026-07-07
Проверено: bot_api vs UI. Модуль уже широкий (auto_replies/воронки/подписчики/расписания/deeplinks/команды/профиль/роль/релей/multigeo/webhook).
Найдено: `bot_api.set_photo`/`delete_my_photo` (аватар бота) есть, но UI профиля не использовал — единственная явная мёртвая ширина.
Исправлено: эндпоинт `bot_avatar` (POST по https-URL → скачать с потолком 5МБ+таймаут+проверка content-type → setMyPhoto; DELETE → deleteMyPhoto). SSRF-гард `is_safe_public_url` (только https, отсекает localhost/приватные IP/*.internal/*.local — тест). UI: поле URL аватара + кнопки поставить/удалить в модалке профиля. Регресс-тест `tests/test_ssrf_guard.py`. Осталось (бэклог): показ текущих значений профиля при редактировании (get_my_description), menu button (в bot_api нет обёртки), массовые операции над ботами.

## МОДУЛЬ Прокси — глубина+ширина до топ1 (волна 2) — 2026-07-07
Проверено: user_proxies + proxy_selector vs UI.
Найдено: крупнейшая мёртвая ширина волны 2. `account_manager.test_proxy` и `proxy_selector.check_proxy_health` есть, схема user_proxies имеет is_alive/last_check — но проверить прокси из UI было нельзя (только add/delete/list). Массового импорта не было (по одному).
Исправлено: async-примитив `proxy_selector.probe_proxy` (форс-проверка через api.telegram.org, aiohttp — не блокирует loop, в отличие от test_proxy с блокирующим сокетом). Эндпоинты: проверка одного (persist is_alive/last_check), проверить все (bounded concurrency 10), массовый импорт списком. UI: кнопка 🔄 у каждого + латентность + время проверки, «Проверить все», «Импорт списком». Валидатор схемы вынесен в `parse_proxy_type` (единый для add+import, тест). Регресс-тест `tests/test_proxy_type.py`. Осталось (бэклог): авто-распределение прокси по аккаунтам (1 на аккаунт), авто-ротация при деградации в UI.

## МОДУЛЬ Парсер аудитории — глубина+ширина до топ1 — 2026-07-07
Проверено: parser.py + parsed_audiences vs UI (см. PARITY_GAP_ANALYSIS).
Найдено: схема parsed_audiences богатая (is_premium/is_bot/is_active/last_seen_days/geo/phone), но get_parsed_audience фильтровал только active_only, а UI показывал плоский список без срезов и экспорта. `parse_active_users(days_back)` умел окно активности — UI хардкодил 30.
Исправлено: days_back выведен в модалку (показывается для режима «активные»), проведён в parse_active_users. Просмотр аудитории: фильтр-чипы (premium / с username / с телефоном / активные / не боты) + счётчики срезов + CSV-экспорт (тот же фильтр). Фильтр вынесен в `parsed_audience_filters` (чистая функция, тест). Регресс-тест `tests/test_parsed_audience_filters.py`. Осталось (бэклог): парс комментаторов/реакций (enum source_type подразумевает, кода нет), гео-фильтр в UI, вычитание списков (A минус B), конвейер «парс→инвайт/DM» одной кнопкой из экрана парсера.

## МОДУЛЬ Инвайтер — глубина+ширина до топ1 — 2026-07-07
Проверено: mass_inviter_engine + _exec_mass_invite vs UI (см. PARITY_GAP_ANALYSIS).
Найдено: бэкенд умел invite_by_phones и batch_size, но UI давал 3 источника-таблицы и ноль настроек безопасности. Пауза между батчами хардкод 3с. `invite_by_phones` (инвайт по номерам) недоступен из UI.
Исправлено: темп (pace → множитель паузы), max_invites (глобальный стоп за прогон), per_account_limit (лимит на аккаунт ЗА ПРОГОН — честно, не «в день»), batch_size в UI, источник import_list (свой список: @username/ID/телефон; разбор взаимоисключающий — телефон только с '+', иначе числовой ID попал бы и в refs, и в phones → двойной инвайт). Регресс-тест `tests/test_inviter_import.py`. Осталось: мягкий инвайт (ЛС со ссылкой вместо force-add — крупная отдельная фича), фильтр аудитории (только с username / дедуп уже приглашённых), авто-стоп при пороге PeerFlood, дневной лимит (нужна привязка к operation_audit).

## МОДУЛЬ Управление каналами — глубина+ширина до топ1 — 2026-07-07
Проверено: возможности account_manager по каналам vs что выведено в UI (см. docs/PARITY_GAP_ANALYSIS.md).
Найдено: крупнейшая мёртвая ширина из трёх модулей. `account_manager` умеет edit_channel_title, get_channel_invite_link, get_channel_members, kick_from_channel, delete_channel, get_full_channel_info — а UI редактора канала давал только about/username. Смена НАЗВАНИЯ канала была недоступна, хотя код есть.
Исправлено (этот проход): смена названия канала — одиночная (channel_edit op=title) и массовая (channels_mass op=title), проведена в воркер (bulk_chan_exec op=chan_title, персист в managed_channels); инвайт-ссылка — инлайн-эндпоинт `/channel/{id}/invite_link` (get_channel_invite_link, один Telethon-вызов, копирование в буфер в UI). Маппинг op→worker_op вынесен в `channel_edit_worker_op` (единый источник, тест). Регресс-тест `tests/test_channel_edit_ops.py`. Осталось (бэклог, в PARITY_GAP_ANALYSIS): список/кик участников, удаление канала (с двойным подтверждением), просмотр/удаление постов, ротация инвайт-ссылок, slow mode/sign messages.

## МОДУЛЬ Авто-ответы (auto_responder + auto_replies) — углубление до паритета — 2026-07-07
Проверено: настройки правила auto_replies против паритета автоответчиков.
Найдено: правило умело только trigger_type/keyword/match_mode/buttons. Не было: приоритета (при нескольких совпадениях «первое по id» — недетерминированно для пользователя), рабочих часов, задержки ответа, аналитики срабатываний (хотя `auto_reply_log` уже писался движком — но нигде не показывался).
Исправлено: да. schema_v142 добавил reply_delay_sec, active_from_hour, active_to_hour, priority. Движок: правила теперь сортируются по priority DESC (первое совпадение выигрывает → приоритет решает), проверяется рабочее окно (вне окна правило молчит; чистый хелпер `_within_active_window` с поддержкой окна через полночь), применяется задержка перед ответом. Эндпоинт создания принимает новые поля (клампы: delay 0..300, hour 0..23, priority ±100, окно — оба часа или ни одного). Список правил теперь показывает счётчик срабатываний (LEFT JOIN auto_reply_log) + приоритет/задержку/окно. UI-модалка: блок «Дополнительно». Регресс-тест `tests/test_auto_reply_window.py`. Осталось (следующий проход): медиа-ответ (фото/файл), регэксп-совпадение, per-user cooldown/лимит срабатываний, таймзона окна (сейчас UTC), configurable multi-fire (сейчас всегда «первое совпадение выигрывает»).

## МОДУЛЬ DM-рассылки (dm_engine + composer) — углубление до паритета — 2026-07-07
Проверено: два параллельных композера vs возможности dm_engine.
Найдено: (1) **дубль-композер** — тонкий openDmModal/submitDmCampaign (имя/текст/2 аудитории) и богатый cmp* (когорта/темп), оба POST на один эндпоинт — «duplicate feature path» из `.botmother/16`. (2) `dm_engine._get_targets` поддерживает 6 типов таргета (bot_users, cohort, all_bots, crm, parsed_audience, import_list), но UI выводил максимум 3 — crm/parsed_audience/import_list были рабочими в движке и невидимыми в UI. (3) `expand_spintax(template)` вызывается на каждого получателя — inline-spintax в DM **уже работал**, но не было ни подсказки, ни превью. (4) Не было лимита отправок на аккаунт (защита от бана).
Исправлено: да. Схлопнул в один композер (удалил openDmModal/submitDmCampaign + легаси-модалку, «+ Новая» → openCmpModal). Вывел все 6 типов таргета. Добавил import_list (разбор `dm_engine.parse_import_list`), лимит на аккаунт в день (честный: префилл сегодняшних отправок из dm_campaign_log через все кампании владельца; аккаунт сверх лимита выбывает; все выбыли → пауза — `pick_account_under_cap`), spintax-превью в композере. Клампы по безопасности (per_account_daily 1..200). Регресс-тесты `tests/test_dm_engine_helpers.py`. Осталось (следующий проход): медиа/инлайн-кнопки в DM (send_dm сейчас text-only), авто-возобновление на следующий день после дневного лимита (сейчас ручной релонч), расписание/окна отправки.
Побочно найдено (НЕ этой сессии, пре-существующее, в origin): `mini_app/index.html` объявляет `toggleFunnel` дважды (стр ~4956 и ~11261) — второе `async function` затеняет первое, один из двух путей воронок мёртв. Требует отдельного разбора (разные сигнатуры id/el vs fid/btn — надо понять, какой вызыватель какой ждёт).

## tg-manager/services/spintax_ai.py, spintax_engine/*, spintax_service.py — 2026-07-06
Проверено: лексер/парсер/генератор spintax, подсчёт вариантов групп, unique-pool через structural_key, seeded RNG, интеграция spintax_service ↔ spintax_engine.
Найдено: ничего существенного.
Исправлено: н/д.

## tg-manager/services/strike_engine.py — 2026-07-06
Проверено: staggered_strike/отмена операции, smart_detect_preset.
Найдено: smart_detect_preset содержит мисклассификацию (насилие путается с терроризмом), но функция мертва — нет вызывающих. Низкий приоритет, пока не появится вызывающий код.
Исправлено: нет (не критично, зафиксировано здесь).

## tg-manager/services/resource_selector.py — 2026-07-06
Проверено: select_account_rotated и in-memory usage tracking.
Найдено: состояние процесс-локальное, не переживает рестарт/не шарится между воркерами; сейчас не используется в реальных диспетчерах (только в тестах) — риск станет реальным только при подключении к продовой логике.
Исправлено: нет (не критично сейчас, зафиксировано здесь + в CLAUDE.md п.6).

## tg-manager/services/session_importer.py — 2026-07-06
Проверено: validate_session(), import_sessions().
Найдено: proxy_url принимался и не передавался в _make_client; клиент не отключался при ошибке get_me() после успешного connect() (утечка соединения).
Исправлено: да — коммит `fix(sessions): honor proxy_url in validate_session + close leaked client`; регресс-тест `tests/test_session_importer_validate.py`.

## tg-manager/mini_app/index.html + services/mini_app_api.py + services/op_worker.py (фичи волны xfAh6) — 2026-07-06
Проверено: broadcast resend, account cluster/label, warmup pause/resume/cancel, channel growth stats + pin, DM cohort targeting, auto-reply inline buttons, Quick Post scheduling, deferred publish, proxy assignment, bot management — полная цепочка UI → route → handler → DB → tenant scope для каждой фичи.
Найдено: (1) `/api/miniapp/channel/add` не существовал — кнопка Quick Channel Add 404-ила с момента мерджа; (2) `auto_responder._match_rule` не работал с мульти-ключами в match_mode=exact/starts. Кросс-тенантных утечек не найдено — все новые эндпоинты корректно скоупятся по owner_id.
Исправлено: да — коммит `fix(mini-app): implement missing /channel/add endpoint + fix multi-keyword auto-reply`; регресс-тесты `tests/test_auto_responder_match.py`.

## tg-manager/services/mini_app_auth.py — 2026-07-06
Проверено: validate_init_data (HMAC-SHA256 подпись Telegram initData), make_token/parse_token (сессионные токены).
Найдено: ничего существенного — constant-time сравнение, корректный HMAC, разумный TTL.
Исправлено: н/д.

## tg-manager/tests/conftest.py (тестовая инфраструктура) — 2026-07-06
Проверено: почему ~90 из ~700 тестов не собирались/падали при обычном запуске pytest.
Найдено: стабы aiogram/asyncpg отстали от реального использования кода (нет BaseMiddleware, F — класс вместо инстанса, нет части aiogram.types, нет asyncpg.connect); плюс requirements.txt не ставится в чистом окружении (падает сборка нативного расширения одной из telethon-зависимостей).
Исправлено: да — стабы обновлены, метакласс `_AnyMeta` добавлен (аккуратно исключая dunder-атрибуты, чтобы не ломать inspect.signature — см. `tests/test_create_pool_kwargs.py`, помечен skip при застабленном asyncpg вместо ложного прохождения/падения). Итог: 600+ тестов реально проходят вместо ~90 незаметных провалов. Проблема с компиляцией нативного расширения в requirements.txt не чинилась (это окружение, не тестовый код).

## tg-manager: session_str / proxy_url хранение — 2026-07-06
Проверено: шифруются ли Telegram-сессии и прокси-креды в БД, как того требуют `.botmother/15` и `docs/SECURITY.md`.
Найдено: **plaintext** — `tg_accounts.session_str` и `user_proxies.proxy_url` нигде не проходят через encrypt/decrypt, хотя `services/token_vault.py` (AES-256-GCM) уже реализован и используется для bot-токенов. Самый серьёзный разрыв документация↔реальность из найденных.
Исправлено: нет — требует миграции + decrypt-on-read на ~30+ точках чтения, высокий blast radius на горячем проде. Зафиксировано в CLAUDE.md, ждёт отдельного спланированного захода.

## tg-manager: session_str шифрование at-rest — РЕАЛИЗОВАНО — 2026-07-07
Проверено/сделано: закрыта session-половина разрыва выше. `tg_accounts.session_str` теперь шифруется AES-256-GCM (token_vault, префикс `ENC:`) на всех 4 точках записи (`db.add_tg_account`, `session_importer`, `accounts.py` re-auth ×2, `auto_registrar`); decrypt — в единственной точке потребления `_make_client` (passthrough для legacy-plaintext → миграция ленивая, без простоя). Подводный камень: шифр недетерминирован (случайный nonce) → дедуп по равенству session_str ломался; добавлен детерминированный `session_fp = sha256(plaintext)` (schema_v143) + дедуп в импортёре по `session_fp OR legacy plaintext`.
Верификация: реальный PostgreSQL 16 — сессия пишется с `ENC:`, decrypt возвращает исходник, fingerprint совпадает, upsert без дублей; 657 тестов + 7 новых регресс-тестов (`tests/test_session_encryption.py`).
Осталось follow-up: `proxy_url` в `user_proxies`/`tg_accounts` (читается в ~8 разрозненных местах — отдельный заход) и `booster_sessions.session_str`.

## tg-manager: schema_v* миграции — ошибки применения — 2026-07-07
Проверено: применение всех schema_v*.sql на чистом PostgreSQL 16 (create_pool), поиск молча падающих миграций.
Найдено (2 файла роняли часть миграции на КАЖДОМ свежем деплое): (1) `schema_v77` — `REFERENCES users(id)`, таблицы `users` не существует (каноническая — `platform_users`) → НИ ОДНА из 6 gift_* таблиц не создавалась, фича Gift Transfer была полностью мертва в проде (нет таблиц). (2) `schema_v136` — частичный индекс с предикатом `NOW()` (не IMMUTABLE) + индексы на `channel_members`, которая создаётся позже (v137) → "relation does not exist".
Исправлено: да — v77 `REFERENCES platform_users(user_id)`; v136 плоский индекс + перенос channel_members-индексов в новый v144. Верификация: свежая БД → 0 ошибок миграции, все 6 gift-таблиц и все индексы создаются. Регресс: `tests/test_schema_lint.py` (3 статических правила: нет users(id); нет volatile-функций в предикате индекса; индекс не раньше создающей таблицу схемы).

## tg-manager: proxy_url шифрование at-rest — ТРАССИРОВАНО, дизайн готов, НЕ реализовано (риск изоляции) — 2026-07-07
Проверено (полная трассировка чтения/записи/потребления proxy_url): в отличие от session_str (чистый choke point _make_client), proxy_url — ОБЩИЙ МЕЖТАБЛИЧНЫЙ КЛЮЧ:
  - user_proxies: UNIQUE(owner_id, proxy_url) + ON CONFLICT(owner_id, proxy_url) — mini_app_api.py:6596, proxy_manager.py:329;
  - infra_memory_proxies: PRIMARY KEY (proxy_url, action_type) — память перф-статистики прокси;
  - platform_proxy_pool: ON CONFLICT (proxy_url) — но это ПУБЛИЧНЫЕ спарсенные прокси, не секрет;
  - db_maintenance: cross-join user_proxies.proxy_url = infra_memory_proxies.proxy_url;
  - потребление: _parse_proxy (connect), test_proxy (→_parse_proxy), extract_ip_from_proxy (regex @host: для прокси-ИЗОЛЯЦИИ — ядро продукта).
Вывод: недетерминированный AES-GCM (token_vault) структурно несовместим — рвёт UNIQUE/PK/ON CONFLICT/cross-join; наивное шифрование одной таблицы ломает db_maintenance cross-join и рискует рассинхроном ключей infra_memory → бьёт по слою изоляции (по CLAUDE.md — дороже всего сломать: баны). Секрет — только креды user:pass внутри user_proxies.proxy_url; host:port не секретны (и служат ключом изоляции).
Безопасный дизайн (для отдельного спланированного захода): (1) детерминированный proxy_fp (sha256 plaintext) + UNIQUE(owner_id, proxy_fp), ON CONFLICT → proxy_fp на обоих write-points; (2) encrypt proxy_url только в user_proxies; (3) decrypt-passthrough в _parse_proxy + extract_ip_from_proxy; (4) переписать db_maintenance prune: тянуть активные user-прокси, decrypt в Python, чистить infra_memory по множеству plaintext (SQL-equality join невозможен на шифре); (5) platform_proxy_pool и infra_memory_proxies keys оставить plaintext (публичные/connection-time значения). Требует верификации на реальном PG: roundtrip, _parse_proxy, extract_ip корректный IP, ON CONFLICT дедуп, db_maintenance не удаляет лишнее.
Статус: НЕ реализовано — ждёт go/no-go (изменение слоя изоляции, blast radius = баны). Session-половина разрыва №1 — закрыта (см. запись выше).
