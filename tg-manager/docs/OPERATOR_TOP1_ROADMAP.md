# Operator Top-1 Roadmap — вывод инфраструктуры в топ поиска Telegram + сетки

Взгляд владельца большой сетки каналов/чатов/ботов, задача — вывести инфраструктуру
в топ поиска Telegram и легко вести сетки. Сопоставление с TeleRaptor / TgSoft /
Telegram Expert и др. Ниже — что УЖЕ есть (не строить заново) и чего НЕ хватает до
Top-1, с приоритетами. Обновлять по мере закрытия (как AUDIT_LEDGER).

Граница: НЕ строим массовые ложные жалобы/takedown чужих ресурсов (см. ledger «Strike
граница по этике»). Всё остальное — да.

## Что уже есть (проверено по коду)
- **Поиск-ранк**: `ranking_checker` (позиции keyword×бот, on-demand + фон), `search_observer`
  (снапшоты/события/осцилляция/cooldown), **алерты падения/роста позиции** (ranking_checker:338,
  notify_settings), `niche_searcher` (AI-ключи + поиск ниши), `global_search_engine` (глобал-поиск).
- **SEO-оптимизация**: `seo.py` — AI-генерация title/about/username с ключом + **apply_title/apply_all**
  (реально применяет через Telethon), `seo_ai_suggestions`.
- **Сетки/оркестрация**: `ecosystem_brain` (экосистемы+здоровье), `content_mesh` (контент-сеть),
  `narrative_engine` (кросс-сетевые нарративы), `presence_planner`/`presence_setup`, `topology`,
  `infra_orchestrator` (готовность к операции), `swarm`.
- **Рост**: boost (views/reactions/subscribers/stories/bot_starts), warmup, self_promo, invite, parser.
- **Фабрики**: каналов/групп/ботов, mass_publish, quick_post, presence packs, шаблоны ассетов.
- **Инфра**: op_worker (потоки/floodwait/лог/отчёт), resource_selector, account_budget,
  session_simulator, proxy-изоляция + backup/failover, шифрование сессий/прокси.

## Пробелы до Top-1 (приоритезировано)

### P0 — прямое влияние на топ поиска и на «легко вести сетку»
1. **Autopost v2 — рекуррентные посты в СВОИ каналы** [✅ СДЕЛАНО]
   op_worker: после успешного done постинг-op'а (allowlist `_RECURRING_OK_OPS`) с
   `repeat_interval_min>0` ставится следующий с `scheduled_for=now()+interval`
   (опц. `repeat_count` декрементится). Эндпойнты quick_post (каналы) и
   run_broadcast (боты) принимают `repeat_interval_min`; UI — селектор «Повтор».
   Тест `test_autopost_v2_recurring.py`. Было:
   Сейчас recurring есть только для managed-бот рассылок (`scheduled_broadcasts.repeat_interval_min`),
   НЕ для постов в каналы через `operation_queue`. Активность канала = сигнал ранжирования и
   ядро ведения сетки. Фикс: параметр `repeat_interval_min` у quick_post/mass_publish op; при
   успехе — переочередь следующего запуска (зеркало `db.reschedule_if_recurring` на operation_queue).
2. **Search Rank Campaign — оркестратор вывода в топ по ключу** [план]
   Все части есть по отдельности (SEO-оптимизатор, boost, постинг, ранк-трекер), но нет ЕДИНОГО
   сценария: цель-ключ → оптимизировать метаданные + добрать подписчиков + поднять активность +
   мерить прирост позиции. Это ключевая топ-1 фича, которой добиваются TeleRaptor/Expert.
3. **Network Rank Dashboard — единый обзор позиций по всей сетке** [план]
   Ранк-трек сейчас по-ботно; оператору нужен один экран: все ключи × все сущности + тренд + дельта.

### P1 — масштаб и скорость сетевых операций
4. **Bulk SEO по сетке** — применить SEO-оптимизацию к N каналам за раз (сейчас по-канально).
5. **Per-account analytics view** — раздел 16 «По аккаунтам» отсутствует как вид (флаг аудита).
6. **Rotating proxy** — ротация прокси по расписанию (сейчас только статическая привязка/failover).
7. **Proxy export / cleanup dead** — экспорт списка и удаление мёртвых user-прокси (аудит р.13).
8. **GPT авто-ответ** — мёртвый бэкенд `auto_responder.py:659` (нет кнопки act_ai_reply) — авто-прогрев ЛС.

### P2 — удобство/полнота
9. **Ручное добавление контакта** в Unified Hub (мёртвый `upsert_contact`).
10. **«По прокси» аналитика** — `proxy_stats` есть, UI-вызова нет (мёртвый бэкенд).
11. **Автопланировщик очистки контактов** (сейчас только вручную).

## Порядок реализации
P0 → P1 → P2. Каждый пункт: сервис/движок → op/эндпойнт → UI → тест → запись сюда «сделано».
