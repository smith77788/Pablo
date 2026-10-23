# Dead-buttons audit — проверенный список (2026-07-10)

Метод: статический скан всей кодовой базы + **ручная верификация каждого кандидата**
(автоскан давал 53 ложных — почти все «мёртвые» на деле обработаны через
`F.action.in_({...})`-сеты, многострочные фильтры и динамическую регистрацию
`F.action == var`). Ниже — только ПОДТВЕРЖДЁННЫЕ находки после отсева ложных.

Итог по классам:
- Mini-app `onclick` → неопределённая JS-функция: **0** (чисто).
- Phantom-op (submit op_type без исполнителя в op_worker): **0** (было 3 — reauth_account/
  export_session/reset_cooldown, ИСПРАВЛЕНЫ 2026-07-10, см. AUDIT_LEDGER).
- Bot callback без хендлера: **7** (кластер bulk-меню каналов).
- Mini-app вызов api() → незарегистрированный роут: **~4 экрана** (Network/Workflow/
  Audience/Analytics — контракт-рассинхрон, чужие полосы).

«Очень много» из ощущения пользователя по факту = ~11 реальных (не сотни); остальное —
ложные срабатывания скана. Точный перечень ниже, с указанием куда каждую подключить.

---

## A. Bot: 7 мёртвых кнопок в bulk-меню каналов — ✅ ИСПРАВЛЕНО 2026-07-10
Фикс: добавлен `cb_bulk_menu_entry` (channel_ops.py, перед cb_bulk_confirm_selection) —
маршрутизирует все 7 action → `_show_bulk_select(op)`. Вся downstream-логика (выбор
аккаунтов → cb_bulk_confirm_selection → op dm/post/chan_uname/chan_about/prof_*) УЖЕ
существовала и покрывает эти op-коды — не хватало только проводки входа. Тест
`tests/test_bulk_menu_entry_wired.py`. Ниже — исходная таблица (для истории).


Файл `bot/handlers/channel_ops.py`, клавиатура bulk-меню (~строки 402–431), показывается
хендлером `ChanCb.filter(F.action == "bulk_menu")`. Кнопки создаются, но **нет ни одного
`@router.callback_query(ChanCb.filter(...))` на этот action** → клик = молчаливый no-op.

| Кнопка (текст) | action | Куда подключить (op УЖЕ существует) |
|---|---|---|
| 📤 Пост с нескольких аккаунтов | `bulk_post` | op `bulk_post_to_channel` (op_worker) |
| ✉️ DM по username-списку | `bulk_dm` | op `bulk_dm_adhoc` (op_worker) |
| 🔤 Username каналам (bulk) | `bulk_chan_uname` | op `bulk_edit_channels` |
| 📄 Описание каналам (bulk) | `bulk_chan_about` | op `bulk_edit_channels` |
| ✏️ Имя аккаунта (bulk) | `bulk_prof_name` | op `bulk_update_profile` |
| 📝 Bio аккаунта (bulk) | `bulk_prof_bio` | op `bulk_update_profile` |
| 🔤 Username аккаунта (bulk) | `bulk_prof_uname` | op `bulk_update_profile` |

Тип фикса: для каждой — хендлер, который принимает ввод (FSM, по образцу рабочего
per-account `prof_*` в том же файле, регистрация ~строка 3293) и ставит существующий op.
Лейн: «управление каналами» (bulk_chan_*/bulk_post/bulk_dm) — Агент B; «профиль аккаунтов
bulk» (bulk_prof_*) — аккаунт-ops. Не берём с наскока (файл активно правит Агент B):
координировать, чтобы не конфликтовать. Все backend-op'ы готовы — это чисто проводка UI→op.

## B. Mini-app: мёртвые экраны — ✅ ИСПРАВЛЕНО 2026-07-10 (взял все 4 end-to-end)
Все 4 раздела оживлены: dashboard_realtime, audience_analytics, plural networks-CRUD,
plural workflows-CRUD + фикс 500 в workflow_list. См. AUDIT_LEDGER. Ниже — исходный разбор.

Уже зафиксировано ранее в AUDIT_LEDGER (запись про dead-routes), подтверждено этим сканом:
- **Network Builder**: фронт зовёт `/api/miniapp/networks`, `/networks/{id}/nodes`,
  `/networks/{id}/edges`, `/networks/nodes/{id}`, `/networks/edges/{id}` (**plural**);
  бэк регистрирует `/network`, `/network/instance/{id}`, `/network/templates` (**singular**)
  → все data-вызовы экрана 404.
- **Workflows**: фронт `/workflows/{id}`, `/workflows/{id}/steps`; бэк `/workflow/{id}`,
  `/workflow/{id}/status`, `/workflow/create|execute|pause|resume` → detail/steps 404.
- **`/api/miniapp/audience_analytics`** и **`/api/miniapp/dashboard_realtime`** — фронт
  вызывает, бэк НЕ регистрирует (0 роутов). Экраны Audience/Analytics-realtime без данных.
Владельцам (A/C): выбрать канонический контракт и привести вторую сторону. Не беру
(активные полосы 2 агентов), чтобы не конфликтовать.

### B.1 Точный контракт-разрыв (turnkey-спец, проверено 2026-07-10)
Разрыв НЕ только в путях (plural↔singular) — ещё и в ФОРМЕ ответа И в самом вызове
фронта. Чинить = владеть ОБЕИМИ сторонами (иначе фейк-экран). Детали:
- **Analytics Dashboard** `loadDashboard()` (index.html ~15148): зовёт
  `api('/dashboard_realtime', {method:'?'+params})` — БАГ (method вместо URL);
  роут НЕ зарегистрирован; фронт читает `d.total_subscribers/total_views/total_posts/
  total_channels/top_channels/subs_history/views_history/engagement_history/
  growth_*/recent_activity`. Движок `analytics_dashboard.get_dashboard_stats()` даёт
  часть, `get_realtime_metrics()` даёт ДРУГОЕ (active_operations/queue_depth) — форма
  не совпадает ни с одной. Нужен новый агрегат owner-уровня под эти поля.
- **Audience Analytics** `loadAudienceAnalytics()` (~15000): зовёт
  `/audience_analytics` (роут НЕ зарегистрирован); фронт ждёт owner-агрегат
  `{total_users,active_users,avg_engagement,segments[],insights[],heatmap[]}`. Движок
  `audience_analytics.py` — ПО-КАНАЛЬНО (analyze_audience(channel_id)), owner-агрегата
  нет. Нужна агрегация по каналам владельца.
- **Network Builder**: фронт `/networks`(GET/POST), `/networks/{id}/nodes`,
  `/networks/{id}/edges`, `/networks/nodes/{id}`, `/networks/edges/{id}`; бэк
  `/network`, `/network/instance/{id}`, `/network/templates`, `/network/template`.
  Нужны plural-CRUD узлов/рёбер поверх `network_builder.py`.
- **Workflows**: фронт `/workflows/{id}`(GET), `/workflows/{id}/steps`; бэк
  `/workflow/{id}`, `/workflow/{id}/status`, `/workflow/create|execute|pause|resume`.
  Нужны detail+steps под контракт фронта.
Вывод: это не «дописать кнопку», а достроить 4 раздела end-to-end (фронт+бэк). Крупно,
активно строится параллельным агентом (свежий большой пуш) → делать только владея обеими
сторонами, по согласованию, чтобы не продублировать/не конфликтовать.

## C. Не-мёртвое, но выглядело подозрительно (проверено — OK)
- `MyCb(action="chosen")` (`bot/utils/target_selector.py`) — переиспользуемый виджет
  выбора цели; хендлер регистрирует потребитель виджета. Не мёртвая.
- `apply_all/apply_title/apply_about/apply_uname/chan_apply` (SeoCb) — обработаны
  многострочным `F.action.in_({...})` (seo.py:1713). Живые (скан давал ложное).
- `prof_name/prof_bio/prof_uname` (ChanCb) — динамическая регистрация `F.action == _prof_action`
  в цикле (channel_ops.py:3293). Живые.

---

## D. «Не существующие кнопки, которые реально должны быть» (missing)
- **Mini-app: ввод номера + релог прямо из карточки** — сейчас session_expired ведёт в бота;
  релог с ручным вводом номера уже сделан в БОТЕ (2026-07-10), в mini-app — кнопка-редирект.
  Кандидат: полноценный релог-флоу в mini-app (запрос кода → ввод) — крупно, отдельно.
- **Ротация прокси по расписанию** — ручная ротация сделана (`/api/miniapp/proxy/rotate`);
  кнопки «включить авто-ротацию каждые N часов» нет. Кандидат (нужен scheduler-интеграция).
- **Bulk-переавторизация списком в mini-app** — есть бейдж session_expired и переход в бота;
  единого экрана «все требующие релога + перейти» в mini-app нет (в боте — Переавторизатор).
- **Bot-паритет операторских модулей** — ranking/network/workflow/analytics/ai_comment
  доступны только в mini-app, в боте кнопок нет (зафиксировано в OPERATOR_TOP1_ROADMAP).

## Как перепроверять (чтобы не ловить ложные)
Хендлер может быть скрыт под: `F.action.in_({...})` (СЕТ, не список!), многострочный
фильтр, `F.action == variable` в цикле динамической регистрации, `F.action.startswith(...)`,
ветвление в теле по `callback_data.action`. Скан обязан учитывать ВСЕ эти формы —
иначе даёт десятки ложных «мёртвых» (как первый проход: 53 → после отсева 7).
