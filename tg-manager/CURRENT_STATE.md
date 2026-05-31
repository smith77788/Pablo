# CURRENT STATE

Обновлено: 2026-05-31 (r17)

## Статус: ВСЕ КРИТИЧЕСКИЕ БАГИ r17 ИСПРАВЛЕНЫ + НОВЫЕ ФИЧИ

### ✅ Выполнено в r17 (2026-05-31)

1. **Strike Engine critical crash fix (inline)**
   - `_escalate_to_spambot()` возвращает `bool`, а не dict
   - Исправлена строка 575–579 в `strike_engine.py`: `isinstance(spambot_result, dict)` check
   - Добавлено поле `mode: str = "normal"` в `StrikePlan` dataclass
   - `_one_account_strike` получает `mode` параметр с тремя ветками kwargs (fast/normal/maximum)
   - Все три волны в `staggered_strike` передают `mode=plan.mode`

2. **Strike режимы Fast/Normal/Maximum (551b806)**
   - `cb_strike_settings` заменён на интерактивный селектор с ✅ чекмарками
   - Три обработчика: `cb_strike_set_mode_fast/normal/maximum`
   - Вспомогательная функция `_set_strike_mode` с UPDATE в БД
   - `schema_v53.sql`: `ALTER TABLE strike_access ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'normal'`
   - `channel_ops.py`: загрузка режима из БД и передача в `StrikePlan`

3. **Импорт .session файлов (551b806)**
   - `account_manager.py`: `import_from_session_file()` — читает SQLite Telethon-сессию, паккует в StringSession
   - `accounts.py`: состояние `waiting_session_file`, кнопка "📂 Session файл (.session)"
   - Обработчики `cb_import_session_file` и `handle_import_session_file`
   - Не требует opentele — чистый `sqlite3` из stdlib

4. **AI ассистент — rate limit backoff (6bbb8c8)**
   - `_call_openrouter`: 2.0s задержка при rate limit (429/rate/limit), 0.5s при других ошибках
   - `_process_ai_turn`: `asyncio.wait_for(timeout=120.0)` + обработка TimeoutError
   - При ошибке AI показывается "🔄 Повторить" + "🏠 Меню" кнопки
   - `cb_ai_retry` обработчик через FSM-историю

5. **opentele error UX + debounce (6bbb8c8)**
   - `handle_import_tdata`: дебаунс флаг `tdata_processing` в FSM
   - При ImportError: inline keyboard с альтернативами (String Session / .session файл / Назад)

6. **Approval workflows (f85c60b)**
   - `schema_v51.sql`: поля `requires_approval`, `approved_at`, `approved_by` в `operation_queue`
   - `bot/handlers/approval_flow.py`: подтверждение/отмена операций с approval guard
   - `op_worker.py`: пропуск ops с `requires_approval=TRUE AND status='waiting_approval'`

7. **RBAC Workspaces Enterprise (64c9b3f)**
   - `schema_v52.sql`: таблицы `workspaces`, `workspace_members`, `workspace_invites`
   - `bot/handlers/workspaces.py`: create, view, members, invite codes, join, leave
   - Enterprise-only gate
   - Кнопка `🏢 Workspaces` в главном меню

8. **Исправления баги (8557263, cb4f77e)**
   - `dm_campaigns.py`: `cb_dm_delete` → `cb_dm_menu` signature fix (missing `state`)
   - `engagement.py`: FSM state leaks — добавлены Cancel кнопки
   - `multigeo.py`: FSM state leaks — добавлены Cancel кнопки
   - `safe_edit` utility в `op_helpers.py`

---

### ✅ КОНСОЛИДАЦИЯ: BotMother OS — единственная точка входа

Все 6 прямых команд заменены на redirect в BotMother OS:
- `/ai` → BotMother → 🤖 AI Assistant
- `/accounts` → BotMother → 🏗️ Infrastructure → 📱 Аккаунты
- `/ops` → BotMother → 🏗️ Infrastructure → 📡 Каналы & операции
- `/ranking` → BotMother → 👁️ Visibility → 📊 Позиции
- `/referral` → BotMother → 💳 Billing → 👥 Referral
- `/subscription` → BotMother → 💳 Billing

### ✅ Выполнено в r16 (2026-05-30)

1. **Behavioral Engine Enhancement — fine-tune formulas (5b92bdc)**
   - Logarithmic scaling вместо линейного: attention, habit, ecosystem, decay
   - schedule_deviation: детекция активности в необычное время суток
   - UI: velocity_spike с ratio, pattern_deviation subtypes, schedule_deviation

2. **Import Center — пре-валидация сессий (2588b75)**
   - _prevalidate_sessions(): проверка base64, длины, пустоты
   - FSM waiting_batch_confirm: отчёт → подтверждение → импорт
   - CSV и .txt пути: единый flow валидация → подтверждение → импорт

3. **Drift Detector fixes + operation reports analysis (d7f09c0)**
   - drift_detector.py: параметры→template (реальная колонка), убран is_active
   - _analyze_error(): анализ причин ошибок и рекомендации в op_detail

### ✅ Выполнено в r15 (2026-05-30)

4. **Template Placeholder Rendering + Inline Help (r15→r16)**
   - auto_responder.py: _render_text() с {{USERNAME}}, {{FIRST_NAME}}, {{DATE}}
   - broadcaster.py: _render_for_user() — батч-загрузка + per-user рендеринг
   - Inline help: keyword-триггер, HTML-форматирование, задержки воронок

5. **Template Validation (0078c07)**
   - template_validator.py: HTML-баланс, лимиты длины, формат username
   - Per-type валидация: bot/chan/group/post/operation

6. **UX Audit: Cancel/Back + Input Validation (8bef2eb)**
   - 6 файлов: auto_reply, funnels, schedule, deeplinks, asset_templates, broadcast
   - 32+ FSM-шагов с Cancel, 14 message-хендлеров с валидацией

7. **Drift Detection + Anomalies UI + Import Center v2**
   - drift_detector.py + schema_v46 (d934241)
   - Anomaly alerts sub-view: decay_spike, affinity_dropout, reentry_burst (d02b247)
   - bulk_chan_uname/about preview/confirm (2b32bdf)
   - CSV import аккаунтов + cluster assignment (6593c17)
   - BulkDm .txt file upload (a34da34)

8. **Template Compare (8ec458a)**
   - _compare_with_templates(): placeholder-aware сравнение
   - 3 вердикта: template_match/partial_match/unexpected
   - template_verdict + matched_templates в restriction_events

9. **Health Dashboard Sparklines (ae9d910)**
   - _make_sparkline(): Unicode block-элементы ▁▂▃▄▅▆▇█
   - _make_comparison_chart: side-by-side сравнение аккаунтов
   - cb_health_sparklines + cb_health_compare

10. **TargetSelector (b046eef)**
    - Reusable target selection abstraction для массовых операций

### ✅ Выполнено в r13-r14

- Account Health Dashboard V2 (тренды, health_score, рекомендации, auto-rotation)
- Global Presence Factory V3 (боты + каналы + группы + пакеты)
- Behavioral Engine (velocity anomaly + pattern deviation)
- AI Assistant реальное выполнение команд
- Bulk pacing (4 режима), file upload, account preview
- UX cleanup, inline help, валидация ввода
- Presence Pack System (schema_v47)
- Strike Engine V2, Deploy Notifier, Topology Map
- CSV Import Center, Drift Detection, Bot Admin Sessions

### 🟡 Осталось (низкий приоритет, r18+)

- Telegram Mini App для аналитики

### 🔄 Текущая ветка
`claude/telegram-bot-services-xfAh6`
Last commit: `551b806 feat: .session file upload + Strike Fast/Normal/Maximum mode selector`

### Проект
- Stack: aiogram 3.13.1, asyncpg, Telethon, Railway
- DB: 60+ таблиц (v53 schema)
- Handlers: 54+ файлов
- Services: 20+ фоновых сервисов
- Ветка: `claude/telegram-bot-services-xfAh6`
- Build: `2026.05.31-r17`
