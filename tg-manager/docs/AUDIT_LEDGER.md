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
