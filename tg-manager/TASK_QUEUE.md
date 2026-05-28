# BotMother Task Queue (v3.2, актуализирован 2026-05-28)

Активная очередь задач для автономной реализации.
Задачи из реального GAP-анализа проекта — не шаблоны.

---

## TASK-001 — Operation Planner FSM

Status: DONE
Priority: P0
Area: telegram-ux / operation-engine
Risk: low

Goal:
Реализовать FSM-wizard для планирования операции на конкретное время.
`op_worker.run()` уже проверяет `scheduled_for` — нужен только UI.

Acceptance Criteria:
- FSM: выбор типа операции → выбор даты/времени → подтверждение
- Записывает в `operation_queue.scheduled_for` (колонка уже есть)
- Показывает список запланированных операций
- Отмена запланированной операции
- Работает из `botmother_menu.py` action="op_planner"

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/mass_ops.py').read())"
- python3 -c "import ast; ast.parse(open('bot/handlers/botmother_menu.py').read())"

Notes:
- CallbackData: `MassOpCb` (уже есть в callbacks.py)
- States: добавить `OpPlannerFSM` в states.py
- entry point: `BmCb(action="op_planner")` в botmother_menu.py

---

## TASK-002 — Notification Delivery

Status: DONE
Priority: P0
Area: safety / monitoring
Risk: low

Goal:
Реализовать реальную доставку уведомлений согласно `notification_settings`.
Сейчас настройки существуют, но уведомления не отправляются.

Acceptance Criteria:
- В `account_monitor.py`: при restriction → check notification_settings.restriction → send
- В `ranking_checker.py`: при position_change → check notification_settings.position_change → send
- В `op_worker.py`: при op_complete → check notification_settings.op_complete → send
- Сообщение понятное: что случилось, с каким активом, что делать
- Никакого спама: не отправлять если пользователь выключил флаг

Required Checks:
- python3 -c "import ast; ast.parse(open('services/account_monitor.py').read())"
- python3 -c "import ast; ast.parse(open('services/ranking_checker.py').read())"
- python3 -c "import ast; ast.parse(open('services/op_worker.py').read())"

Notes:
- `db.get_notification_settings(pool, user_id)` — нужна функция в db.py
- `bot.send_message(user_id, text, parse_mode="HTML")` — стандартный способ

---

## TASK-003 — Post Template → Mass Publish auto-inject

Status: DONE
Priority: P0
Area: telegram-ux / templates
Risk: low

Goal:
При применении post-шаблона автоматически подставлять текст в Mass Publish wizard.

Acceptance Criteria:
- В `asset_templates.py` apply post-шаблона: `state.update_data(tpl_prefill={text, media, ...})`
- В `mass_publish.py` `cb_mpub_start`: проверить `tpl_prefill`, auto-inject текст
- Пользователь видит prefill и может изменить перед отправкой

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/asset_templates.py').read())"
- python3 -c "import ast; ast.parse(open('bot/handlers/mass_publish.py').read())"

Notes:
- Паттерн prefill уже реализован для channel/group шаблонов — повторить для post

---

## TASK-004 — Behavioral Collectors Wiring

Status: DONE
Priority: P1
Area: behavioral / analytics
Risk: low

Goal:
Подключить вызовы коллекторов поведенческого слоя к реальным событиям.

Acceptance Criteria:
- `record_reentry(pool, uid, ...)` вызывается в `start.py` когда пользователь
  возвращается после 7+ дней (сравнить `last_active` с now)
- `record_cross_nav(pool, uid, ...)` вызывается в ключевых точках `botmother_menu.py`
- `record_search_repeat` уже должен вызываться из `ranking.py` — проверить

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/start.py').read())"
- python3 -c "import ast; ast.parse(open('bot/handlers/botmother_menu.py').read())"

Notes:
- `services/behavioral_engine.py` уже содержит коллекторы
- Не добавлять лишних await — коллекторы должны быть non-blocking (asyncio.ensure_future или try/except)

---

## TASK-005 — Operation Builder FSM Wizard

Status: DONE
Priority: P1
Area: operation-engine / telegram-ux
Risk: medium

Goal:
Полноценный FSM-wizard для сборки операции из блоков.

Acceptance Criteria:
- Шаг 1: выбор типа операции (mass_edit_bots / mass_publish / bulk_join / bulk_leave)
- Шаг 2: выбор целей (боты / каналы / аккаунты / по тегу)
- Шаг 3: настройка параметров операции
- Шаг 4: preview с количеством целей и прогнозом времени
- Шаг 5: подтверждение → запись в operation_queue
- Dry-run режим

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/mass_ops.py').read())"

Notes:
- `MassOpCb` + `OpBuilderFSM` в states.py
- Начать с 2 типами операций (mass_publish + bulk_join), остальные добавить позже

---

## TASK-006 — Experiment Conversion Tracking

Status: DONE
Priority: P1
Area: analytics / automation
Risk: low

Goal:
Вызывать `record_experiment_conversion` из `auto_responder.py` когда пользователь
отвечает на сообщение в рамках A/B эксперимента.

Acceptance Criteria:
- `auto_responder.py` при обработке ответа проверяет активные эксперименты для бота
- Если пользователь входит в группу эксперимента → вызвать conversion
- Статистика экспериментов обновляется в реальном времени

Required Checks:
- python3 -c "import ast; ast.parse(open('services/auto_responder.py').read())"

---

## TASK-007 — Visibility Report CSV Export

Status: DONE
Priority: P2
Area: visibility / reports
Risk: low

Goal:
Добавить кнопку "📥 Скачать CSV" в Visibility Reports.

Acceptance Criteria:
- Генерировать CSV файл: keyword, position, checked_at, trend
- Отправлять как документ в Telegram (bot.send_document)
- Кнопка доступна на STARTER+ плане

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/ranking.py').read())"

Notes:
- Использовать `io.StringIO` + `csv.writer` — без записи на диск
- `await bot.send_document(chat_id, BufferedInputFile(data, filename))`

---

## TASK-008 — Search Memory Drill-Down

Status: DONE
Priority: P2
Area: behavioral / visibility
Risk: low

Goal:
Из Behavioral Dashboard → топ keywords → при нажатии показать историю позиций по keyword.

Acceptance Criteria:
- Список из search_memory с affinity_score
- Клик по keyword → история позиций из search_rankings
- Граф тренда (текст-таблица: дата → позиция)

Required Checks:
- python3 -c "import ast; ast.parse(open('bot/handlers/botmother_menu.py').read())"

---

## TASK-009 — Обновить docs после реализации

Status: DONE
Priority: P0
Area: docs
Risk: low

Goal:
После каждой итерации обновить рабочие документы.

Acceptance Criteria:
- CURRENT_STATE.md актуализирован
- IMPLEMENTATION_LOG.md содержит записи о выполненных задачах
- TASK_QUEUE.md статусы обновлены
- docs/FEATURE_INVENTORY.md обновлён
- docs/GAP_ANALYSIS.md обновлён

Required Checks:
- Нет кодовых проверок

---

## АРХИВ ВЫПОЛНЕННЫХ ЗАДАЧ

*(сюда перемещаются DONE-задачи)*

---

## ЗАБЛОКИРОВАННЫЕ ЗАДАЧИ

*(сюда перемещаются BLOCKED-задачи с причиной блокировки)*
