# IMPLEMENTATION LOG

## 2026-05-28 — Global Presence Factory V1

**Цель:** Реализовать полный guided FSM wizard для создания Telegram-присутствия по всему миру.

**Изменённые файлы:**
- `schema_v35.sql` (новый) — таблицы global_presence_plans + global_presence_targets
- `services/geo_data.py` (новый) — 5 пресетов: EU (44), World (51), Tier-1 (50), DACH (20), LATAM (25)
- `services/username_engine.py` (новый) — transliterate, slugify, generate_username_variants
- `services/presence_planner.py` (новый) — render_pattern, build_targets, estimate_duration_minutes
- `bot/handlers/global_presence.py` (новый) — FSM wizard 8 шагов + прогресс + retry + report
- `services/op_worker.py` (изменён) — добавлен global_presence_channel handler
- `database/db.py` (изменён) — 7 новых CRUD функций
- `bot/callbacks.py` (изменён) — добавлен GeoPresenceCb(prefix="gp")
- `bot/states.py` (изменён) — добавлен GlobalPresenceFSM
- `bot/handlers/botmother_menu.py` (изменён) — кнопка 🌍 Global Presence в Operations
- `main.py` (изменён) — роутер зарегистрирован
- `bot/handlers/mass_publish.py` (изменён) — удалено дублирование _progress_text

**До:**
- Global Presence Factory не существовал
- Нет способа создать каналы во всех городах мира

**После:**
- Полный wizard: тип актива → шаблон → название → username → гео → аккаунты → превью → подтверждение
- 5 гео-пресетов из коробки
- Placeholder engine: {{CITY}}, {{COUNTRY}}, {{CITY_SLUG}} и т.д.
- Username engine с transliteration + fallback variants
- Выполнение через op_worker с safe pacing (45-90с между созданиями)
- FloodWait retry автоматически
- Progress tracking, retry failed, final report

**Проверки:**
- `python3 -c "import ast; ast.parse(open(f).read())"` — все файлы OK
- Smoke test: slugify/render_pattern/build_targets работают корректно
- eu_capitals count = 44 ✅

**Риски:**
- set_channel_username требует сессию с доступом к только что созданному каналу — используем get_entity через имеющийся session string
- При большом кол-ве городов (>44) — длительное выполнение (>2ч)

**Следующий шаг:**
Operation Planner FSM — реализовать UI для scheduled_for в operation_queue

---

## 2026-05-27 — Исправление инвайтинга, логина, отмена задач

**Цель:** Исправить критические баги в invite/login операциях.

**Изменённые файлы:**
- `services/account_manager.py` — исправлен invite_users_to_channel (ChatAdminRequiredError, access_hash)
- `bot/handlers/channel_ops.py` — _active_tasks registry, CancelledError handling, human delays
- `bot/handlers/accounts.py` — cb_resend_sms: fresh SendCodeRequest при истёкшем code hash
- `services/op_worker.py` — _is_cancelled helper, cancellation check в bulk_join/bulk_leave
- `bot/handlers/mass_ops.py` — cancel для pending И running операций

**До:**
- invite показывал "0 приглашено из 3734 контактов" (ChatAdminRequiredError тихо игнорировался)
- Вход: "Неверный или истёкший код" (expired phone_code_hash)
- Нельзя отменить запущенную задачу

**После:**
- Invite: ChatAdminRequiredError → немедленная остановка с сообщением об ошибке
- Login: при ошибке resend → новый SendCodeRequest с новым hash
- Отмена: кнопка "❌ Отменить" для запущенных задач + asyncio.Task.cancel()

---

## 2026-05-26 — Device Fingerprints, Behavioral Engine, Session Simulator

**Цель:** Добавить unique device fingerprints для аккаунтов, behavioral analytics, session simulator.

**Изменённые файлы:**
- `schema_v23.sql` — device_model, system_version, app_version в tg_accounts
- `services/account_manager.py` — _ANDROID_DEVICES, generate_device_fingerprint, _make_client c _acc
- `database/db.py` — add_tg_account с device params
- `services/behavioral_engine.py` (новый) — behavioral scores каждые 15 мин
- `services/session_simulator.py` (новый) — human_delay, chaos_factor, typing_delay

**Результат:** Каждый аккаунт имеет уникальный Android fingerprint. Human-like delays в bulk операциях.
