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
