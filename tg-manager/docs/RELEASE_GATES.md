# Ворота релиза

Релиз выпускается, только когда каждая строка ниже отмечена. Строка без
отметки — не «почти готово», а причина не выпускать. Где проверку делает
машина, указан тест или шаг CI; где человек — указано, кто и как.

Прежняя версия (три грепа и смоук) проверяла синтаксис и две привычки кода,
но ничего не говорила о том, работает ли продукт у пользователя. Она в
истории git.

## 1. Код (машина, блокирует)

- [ ] CI `tests` зелёный на том самом коммите, что уезжает в прод: ruff
      (несуществующие имена), полный pytest на живом Postgres 16, храповики
      `test_no_*`, `test_every_*`, `test_stop_reaches_every_executor_loop.py`.
- [ ] В `docs/project_state/OPEN_DEFECTS.md` нет открытых P0.
- [ ] Каждый модуль в `MODULE_STATUS.md`, который виден пользователю в
      релизе, — **ЗАКРЫТ** или **ЧАСТИЧНО** только по звену «живой прогон»
      (строка раздела 3). Модуль со статусом СЛОМАН или НЕ ПРОЙДЕН скрыт из
      меню бота и мини-аппа.
- [ ] Миграции применяются на копии прод-базы без ошибок и без долгих
      блокировок (`create_pool` на копии; лог без `lock_timeout`). Храповики
      порядка и заморозки миграций (`test_migration_order.py`,
      `test_migrations_are_frozen.py`) зелёные.
- [ ] Секретов в репозитории нет: поиск токенов бота
      (`[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}`) вне `tests/` пуст.

## 2. Конфигурация прода (владелец, один раз и при смене окружения)

- [ ] `BOT_TOKEN` (или `MANAGER_BOT_TOKEN`) задан — без него вход в мини-апп
      отключён намеренно.
- [ ] `TOKEN_ENCRYPTION_KEY` задан и сохранён вне Railway: без него
      сессии аккаунтов и прокси в базе не расшифровать.
- [ ] `ADMIN_IDS` заполнен. От него зависят ВСЕ платформенные тревоги
      (падение фонового сервиса, всплеск ошибок, отказы платёжных API, потеря
      защитных записей). Пустой — тревоги уходят только живым сессиям админки,
      а без них — никому.
- [ ] Исполнитель операций ровно один: одна реплика с ролью `all`, либо пара
      `web` + один `worker` (`services/replica_guard.py` предупредит о
      лишнем).
- [ ] Бэкап базы включён (`DB_BACKUP_ENABLED=1` и чат назначения), последний
      бэкап моложе суток, восстановление проверено хотя бы раз
      (`docs/DB_BACKUP.md`).
- [ ] Оплата настроена хотя бы одним способом. Бот берёт каждую настройку
      сначала из «⚙️ Настройка оплаты» (база), потом из переменной окружения
      (`bot/handlers/subscription.py::_pay_cfg`). Без всех способов 12 кнопок
      с ценами ведут в «⚠️ Автоматическая оплата не настроена».

      | Настройка | Что включает |
      | --- | --- |
      | `TRON_WALLET` (+ `TRON_API_KEY` для проверки) | USDT TRC-20: подписка, Массовые жалобы, Host-Server. Минимум для прода: без него последние два показывают только «📩 Запросить доступ» |
      | `TON_WALLET` + `TON_API_KEY` | TON для подписки (только при живом курсе) |
      | `STARS_PER_USD` | Звёзды Telegram (0 или пусто — выключено) |
      | `CARD_PROVIDER_TOKEN`, `CARD_CURRENCY`, `CARD_PER_USD` | Карта через платёжного провайдера Telegram |
      | `CRYPTOPAY_TOKEN` | Восстановление платежей через CryptoPay (`services/payment_recovery.py`) |

      Как убедиться, что оплата работает у пользователя, — раздел 3, строки
      «Оплата».
- [ ] `DEPLOY_WEBHOOK_SECRET` задан, и адрес деплой-хука в Railway кончается
      на `?secret=<то же значение>`. Без переменной `/webhook/deploy` закрыт
      (D31): уведомления о деплое из Railway не приходят.
- [ ] `MONITOR_HEARTBEAT_URL` задан **в каждом сервисе Railway отдельно**
      (у web и worker свой монитор, формат Better Stack Heartbeats;
      Healthchecks.io и Cronitor тоже подходят). Без переменной пульс не
      запускается, и смерть процесса целиком никто снаружи не заметит
      (`services/observability.py`). Период — `MONITOR_HEARTBEAT_EVERY_SEC`,
      по умолчанию 60 с, не меньше 15.
- [ ] `SENTRY_DSN` задан (Sentry или Better Stack Errors). Без него
      `log.exception`/`log.error` остаются только в логах Railway. Пакет
      `sentry-sdk` стоит в `requirements.txt`; если его нет в образе, в логе
      будет `observability: SENTRY_DSN задан, но пакета sentry-sdk нет`.
- [ ] `METRICS_TOKEN` задан, если `/metrics` читает внешний Prometheus/Grafana.
      Без него `/metrics` отвечает только запросам из внутренней сети.

## 3. Живые прогоны (владелец, перед каждым релизом с правками модуля)

Проверено кодом и тестами на Postgres, но не на настоящем Telegram и
блокчейне. Делается на тестовых аккаунтах и своём кошельке.

- [ ] Оплата, настройки: от админа «💳 Подписка» → «⚙️ Настроить кошельки»
      показывает хотя бы один способ. От обычного аккаунта «💳 Подписка» →
      тариф → срок открывает выбор способа, а не «Автоматическая оплата не
      настроена»; «⚔️ Массовые жалобы» → «Купить» и «🖥️ Host-Server» →
      «Купить» показывают реквизиты.
- [ ] Оплата: один платёж TON и один USDT TRC-20 (или звёздами на минимальную
      сумму) доходят до активной подписки и подтверждения пользователю;
      неверная сумма не активирует.
- [ ] Аккаунты: вход по телефону, по QR и по `.session`; повторный импорт
      того же аккаунта не даёт дубля.
- [ ] Прогрев: один день плана проходит и пишется в историю.
- [ ] Восстановление: ручная апелляция получает ответ @SpamBot и честный
      статус.
- [ ] Новичок: с чистого Telegram-аккаунта от `/start` до карточки
      «первый результат».
- [ ] Парсер: группа больше 10 тыс. участников собирается сверх потолка
      выдачи (перебор по буквам, D20); итог совпадает с числом в базе.
- [ ] Массовые операции (инвайт, рассылка): канарейка на 1–3 аккаунтах,
      проверка итога, затем масштаб.

## 4. Наблюдаемость (владелец, после деплоя)

- [ ] `GET /health` и `GET /health/ready` отвечают 200
      (`services/payment_webhook.py`). `ready` проверяет базу и цикл событий —
      ту же проверку делает пульс.
- [ ] Монитор пульса для каждой роли зелёный в течение 5 минут после деплоя.
      Проверка тревоги один раз при настройке: остановить сервис — монитор
      должен поднять тревогу за ≤ 2 мин.
- [ ] Sentry: событие релиза с `release` = коммит деплоя
      (`RAILWAY_GIT_COMMIT_SHA`) и `environment` = окружение Railway; новых
      групп ошибок за первые 30 минут нет или каждая разобрана.
- [ ] Сообщение о деплое (`deploy_notifier`) пришло админу — путь доставки
      платформенных тревог жив.
- [ ] В логах платформы нет строки `platform_alert: сообщить некому`.
- [ ] В access-логе строки запроса с `ticket=` и `token=` показаны как `***`
      (`services/secret_masking.RedactingAccessLogger`).

## 5. Безопасность (машина, блокирует)

Каждый пункт `SECURITY.md` закреплён тестом; красный тест — релиза нет.

- [ ] Каждый маршрут мини-аппа требует входа —
      `test_miniapp_every_route_requires_auth.py`.
- [ ] Данные скоупятся по владельцу — `test_miniapp_owner_scope.py`,
      `test_accounts_owner_scoped_by_default.py`.
- [ ] Токен сессии не ходит в URL, в логах нет секретов —
      `test_session_token_never_in_url.py`, `test_access_log_hides_secrets.py`,
      `test_secret_masking.py`.
- [ ] Вход ограничен по частоте — `test_auth_endpoints_rate_limit.py`.
- [ ] REST API закрыт при сбое проверки ключа — `test_rest_api_auth_fail_closed.py`.
- [ ] Деплой-хук принимает только вызов с секретом —
      `test_deploy_webhook_is_authentic.py`; денежного хука без подписи нет —
      `test_payment_webhook_fail_closed.py`.

## 6. Выпуск и откат

1. PR в `claude/telegram-bot-services-xfAh6`, CI зелёный, влит.
2. Railway выкатывает сам (2–3 мин). Проверка: `/version` в боте.
3. Смоук: `/start` открывает меню; мини-апп грузится, поток «🟢 Подключено»;
   одна безопасная кнопка в каждом из главных разделов без ошибки.
4. Откат: `git revert <коммит> && git push`. Миграции только вперёд, поэтому
   откатываемый код обязан работать на новой схеме — правка, которая это
   ломает, выпускается двумя шагами (сначала схема, потом код).

## 7. Состояние на 2026-11-21: релиз закрыт разделом 1

Известных «не регрессий» нет: каждое падение ниже — причина не выпускать,
пока не починено или не разобрано с записью в `OPEN_DEFECTS.md`.

CI `tests` красный на всех четырёх частях на последних пушах в
`claude/telegram-bot-services-xfAh6` (с run 4477, коммит `3a5790f`, по
`988d9ff`). Локальный прогон на Postgres 16 тем же способом, что в CI
(`INFRAGRAM_TEST_SHARD=N/4`, Python 3.12), на `988d9ff` — 29 падений
в 20 файлах:

- `tests/test_a_target_without_geo_does_not_abort_the_batch_postgres.py` — `test_render_pattern_keeps_its_promise_on_an_empty_column`
- `tests/test_channel_edit_ops.py` — `test_all_worker_ops_distinct`
- `tests/test_channel_reimport_needs_full_scan.py` — `test_complete_list_spares_the_channels_it_carries`, `test_incomplete_list_deletes_nothing`
- `tests/test_every_action_survives_rubbish_in_its_body_postgres.py` — `test_no_action_answers_an_internal_error_for_an_owner_with_data`, `test_no_action_answers_an_internal_error_to_rubbish_in_its_body`
- `tests/test_every_operation_type_has_a_russian_name.py` — `test_every_registered_operation_has_a_russian_name`
- `tests/test_executors_claim_sessions.py` — `test_every_session_executor_claims_its_accounts`
- `tests/test_frozen_queue_e2e_postgres.py` — `test_a_deferred_operation_counts_once_its_time_has_come`, `test_every_affected_owner_is_named`, `test_legitimate_waiting_is_not_a_stall`, `test_the_query_runs_at_all`, `test_the_threshold_is_the_one_from_the_module`
- `tests/test_geo_districts.py` — `test_district_node_renders_via_scope`
- `tests/test_mass_select_all_filtered.py` — `test_ui_sends_select_all_filtered`
- `tests/test_migration_repository_guard.py` — `test_checksum_манифест_строгий_полный_и_без_дублей`, `test_новые_коллизии_числовых_версий_запрещены`
- `tests/test_migrations_are_frozen.py` — `test_новые_миграции_вписаны_в_манифест`
- `tests/test_miniapp_delete_confirm_names_target.py` — `test_call_sites_pass_the_element`
- `tests/test_miniapp_error_does_not_invent.py` — `test_self_loading_screens_offer_a_way_out`
- `tests/test_miniapp_irreversible_asks_first.py` — `test_confirm_names_the_scale`
- `tests/test_miniapp_lists_admit_truncation.py` — `test_capped_routes_return_the_real_total`
- `tests/test_miniapp_operation_status.py` — `test_single_item_callers_wire_poll_op_result`
- `tests/test_miniapp_oplog_search.py` — `test_placeholders_shift_with_the_search_parameter`
- `tests/test_miniapp_reauth_is_shared.py` — `test_every_request_recovers_after_the_token_expires`, `test_the_retry_uses_the_new_token_not_the_old_one`, `test_the_token_is_renewed_once_for_everyone`
- `tests/test_one_session_is_never_used_by_two_operations.py` — `test_every_handler_that_opens_a_session_claims_the_account`
- `tests/test_operation_cleanup_ui.py` — `test_cleanup_confirms_exact_targets_in_chromium`

Явные группы: `schema_v320_ai_autopilot.sql` не вписан в
`schema_checksums.txt` и появилась новая коллизия номера `v371`
(`test_migrations_are_frozen`, `test_migration_repository_guard`); у новых
типов операций нет русского названия и записи в храповиках (по виду —
`contacts_import` и соседние; сверить по выводу теста)
(`test_every_operation_type_has_a_russian_name`,
`test_executors_claim_sessions`, `test_one_session_is_never_used_by_two_operations`,
`test_channel_edit_ops`); проводка мини-аппа (повторный вход, подтверждения,
статусы операций). `test_operation_cleanup_ui` гоняет Chromium и мог упасть
из-за песочницы локального прогона — сверить с CI.

Когда CI снова зелёный, этот раздел удаляется.
