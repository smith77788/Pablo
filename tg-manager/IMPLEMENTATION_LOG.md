# Implementation Log

Claude Code / Codex updates this file after every meaningful task.

---

## 2026-05-28 — TASK-001 — Operation Planner FSM

Goal: Реализовать FSM-wizard планирования операций с записью в operation_queue.scheduled_for

Changed Files:
- bot/states.py — OpPlannerFSM (waiting_text, waiting_datetime)
- bot/handlers/botmother_menu.py — полный wizard вместо заглушки
- services/op_worker.py — _process_pending фильтрует scheduled_for <= now()

Before: cb_op_planner была заглушкой. op_worker игнорировал scheduled_for.

After: Wizard: выбор типа → ввод текста → ввод времени → preview → confirm → INSERT с scheduled_for. op_worker уважает расписание.

Checks Run: ✅ ast.parse() все файлы

Risks: _parse_datetime парсит как UTC. Приемлемо для MVP.

---

## 2026-05-28 — TASK-002 — Notification Delivery

Goal: Реальная доставка уведомлений согласно notification_settings.

Changed Files:
- database/db.py — get_notification_settings(), notify_if_enabled()
- services/account_monitor.py — notify_if_enabled(flood_warning/restriction)
- services/op_worker.py — notify_if_enabled(op_complete)
- services/ranking_checker.py — проверка position_change перед отправкой

Before: уведомления отправлялись всегда, настройки игнорировались.

After: notify_if_enabled() читает настройки из БД, отправляет только если флаг True.

Checks Run: ✅ ast.parse() все файлы

---

## 2026-05-28 — TASK-003 — Post Template → Mass Publish auto-inject

Goal: При apply post-шаблона автоматически подставлять текст.

Changed Files:
- bot/handlers/mass_publish.py — cb_mpub_start и cb_mpub_pick_account: проверка tpl_prefill

Before: tpl_prefill сохранялся, но mass_publish его не проверял.

After: Если в FSM state есть tpl_prefill.text → skip waiting_text, показать timing сразу.

Checks Run: ✅ ast.parse()

---

## 2026-05-28 — TASK-004 — Behavioral Collectors Wiring

Goal: Подключить record_reentry и record_cross_nav к реальным событиям.

Changed Files:
- bot/handlers/start.py — record_reentry при возврате через 7+ дней (ensure_future)
- bot/handlers/botmother_menu.py — _fire_cross_nav в cb_infrastructure/visibility/operations

Before: коллекторы существовали, нигде не вызывались.

After: Reentry пишется при /start после 7+ дней отсутствия. Cross-nav при переходах в разделы.

Checks Run: ✅ ast.parse() оба файла

---

## 2026-05-28 — TASK-007 — Visibility Report CSV Export

Goal: Кнопка "📥 Скачать CSV" в Visibility Reports.

Changed Files:
- bot/handlers/botmother_menu.py — кнопка + cb_vis_reports_csv handler

Before: отчёт только в тексте сообщения.

After: кнопка → файл visibility_report.csv (bot_username, keyword, position, checked_at), UTF-8 BOM.

Checks Run: ✅ ast.parse()

---

## 2026-05-28 — TASK-005 — BulkJoin Wizard

Goal: FSM-wizard для массового вступления аккаунтов в каналы/группы.

Changed Files:
- bot/handlers/mass_ops.py — 3-step wizard (paste links → pick accounts → confirm → enqueue)
- bot/states.py — BulkJoinFSM (waiting_links, choosing_accounts)
- services/op_worker.py — _exec_bulk_join с anti-flood задержкой 30-90s

Before: op_worker не знал bulk_join, нет UI-wizard.

After: Пользователь вставляет ссылки (до 50), выбирает аккаунты, операция ставится в очередь и выполняется с anti-flood задержками.

Checks Run: ✅ ast.parse() все 3 файла

---

## 2026-05-28 — TASK-006 — Experiment Conversion Tracking

Goal: Вызывать record_experiment_conversion при ответе пользователя в рамках A/B эксперимента.

Changed Files:
- services/auto_responder.py — `elif not is_start and active_exp:` вызывает record_experiment_conversion

Before: assign_experiment_variant вызывался на /start, но conversions не фиксировались.

After: Любое не-/start сообщение от пользователя с активным экспериментом → запись конверсии (idempotent, converted=FALSE guard в SQL).

Checks Run: ✅ ast.parse()

---

## 2026-05-28 — TASK-008 — Search Memory Drill-Down

Goal: Из Behavioral Dashboard → топ keywords → история позиций.

Changed Files:
- bot/handlers/botmother_menu.py — кнопки keyword в memory view + cb_mem_keyword_drilldown

Before: keywords только в тексте, не кликабельны.

After: Top 8 keywords — кнопки с BmCb(action="mem_kw"). Drill-down показывает историю позиций из search_rankings JOIN tracked_keywords в виде таблицы.

Checks Run: ✅ ast.parse()

---

---

## 2026-05-28 — TASK-005 (полная реализация) — Operation Builder FSM Wizard

Goal: Полноценный builder операций: все 4 типа, unified menu, bulk_leave как новый тип.

Changed Files:
- bot/states.py — OpBuilderFSM (choosing_op_type), BulkLeaveFSM (waiting_channels, choosing_accounts)
- bot/handlers/mass_ops.py — bulk_leave 3-step wizard (bl_*), меню переименовано в "Построитель операций"
- services/op_worker.py — _exec_bulk_leave с 5-15s задержками

Before: builder показывал 3 типа операций, bulk_leave отсутствовал.

After: 4 типа операций (mass_publish, bulk_join, bulk_leave, bulk_bot_edit). Все через unified entry MassOpCb(action="menu"). OpBuilderFSM и BulkLeaveFSM добавлены в states.py.

Checks Run: ✅ ast.parse() все 3 файла

---

## Следующие задачи

Все задачи из TASK_QUEUE.md (TASK-001 — TASK-009) выполнены. Очередь пуста.
