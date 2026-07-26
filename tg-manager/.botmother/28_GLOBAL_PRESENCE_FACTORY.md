# 28 — Global Presence Factory

Global Presence Factory is a critical Infragram capability.

The user must be able to create Telegram presence across the world in a few guided clicks.

User flow:
1. Open Global Presence menu.
2. Choose what to create:
   - channels
   - groups/chats
   - bots
   - full presence package
3. Choose or create template.
4. Set default title/name pattern.
5. Set default username pattern.
6. Select geography:
   - countries
   - cities
   - regions
   - worldwide presets
   - custom imported geo list
7. Select accounts/account pools.
8. Preview full creation plan.
9. Confirm task.
10. Execute creation wave through Operation Engine.
11. Track progress.
12. Retry failed items.
13. Receive final report.

Core implementation:
Global Presence Factory → Global Presence Plan → Creation Wave → Operation Engine → Progress Tracking → Report → Retry Failed.

Do not implement it as isolated button handlers.

Supported placeholders:
- {{CITY}}
- {{COUNTRY}}
- {{REGION}}
- {{LANGUAGE}}
- {{COUNTRY_CODE}}
- {{CITY_SLUG}}
- {{COUNTRY_SLUG}}
- {{INDEX}}

Username engine must support:
- slug generation
- transliteration where possible
- lowercasing
- invalid character removal
- max length handling
- collision detection
- suffix generation
- availability checks where supported
- fallback variants

Geo object should support:
- country
- country_code
- region
- city
- city_slug
- language
- timezone
- priority
- population if available
- metadata

Safety:
- no execution without preview
- no execution without confirmation
- conservative pacing by default
- account workload balancing
- clear warnings for large plans
- retry failed instead of repeating all
- emergency stop integration if existing

V1 may implement the best-supported asset type first, usually channels, but unsupported types must be marked planned, not fake-complete.

---

## Реализовано: генератор инфраструктуры (2026-07)

Модуль перестал быть «создать канал N раз». Пользователь описывает ПРОЕКТ, а
движок раскрывает его в объекты:

```
География (пресет / свой список / CSV)
        × Уровни (федеральный / региональный / городской)
        × Структура (тематики: новости, чат, работа, афиша, барахолка,
                     недвижимость, авто, услуги, знакомства, бот)
        = дерево целей, у каждой своё имя, username, описание и аватар
```

Ядро — `services/infra_generator.py`:
* `ROLE_LIBRARY` — тематика несёт тип актива и три пула шаблонов
  (названия / username / описания). Чат всегда группа, новости всегда канал.
* `STRUCTURE_PRESETS` — готовые наборы тематик («Ядро города», «Классифайды»).
* `expand_geo_levels` — дедупликация стран и регионов; `{{SCOPE}}` делает один
  шаблон рабочим на всех уровнях.
* `build_project_targets` — сборка целей; `summarize_targets` / `find_duplicates`
  — предпросмотр и самопроверка.

Смежное:
* `services/username_engine.py` — токены `{city} {abbr} {alt} {cc} {role}
  {index} {2} {3} {rand4}`, словари сокращений (msk/spb/ekb) и альтернативных
  написаний, `UsernameAllocator` (коллизии разруливаются на этапе плана).
* `services/ru_morph.py` — `{{CITY_GEN}}` / `{{CITY_LOC}}`: «Работа в Москве»,
  «Новости города Самары». Не уверены в модели — возвращаем именительный.
* `services/avatar_factory.py` — своя картинка каждому объекту, детерминировано
  по seed.

### Инварианты, которые нельзя ломать

1. **Детерминизм по `plan_seed`.** Предпросмотр показывает РЕАЛЬНЫЕ цели плана.
   Если генерация перестанет быть воспроизводимой, превью начнёт врать.
2. **Уникальность до запуска.** username выдаёт аллокатор, знающий и занятые
   имена владельца (`db.get_taken_usernames`), и всё выданное внутри проекта.
3. **Тип актива — по цели, не по плану.** В одном плане уживаются каналы и
   чаты; общий флаг превратил бы половину структуры не в тот тип.
4. **Пулы vs явный отказ.** Пустой пул = «возьми из библиотеки». Отказ от
   username — отдельный флаг `assign_usernames=False`.
5. **Оформление не роняет объект.** Сбой аватара не уводит созданный канал в
   `failed`: объект существует, повторное создание сделало бы дубль.

### Каталог и пакетные операции

* Отчёт показывает фактические username и сколько объектов реально оформлено
  («создано» и «оформлено» — разные числа).
* `📥 Экспорт ссылок` — CSV с ссылками, аккаунтом-создателем и ошибками.
* `gp_bulk_apply` — пакетное применение описаний/аватаров ПО ОБЪЕКТАМ проекта
  (значение считается для каждого отдельно). Отличается от общего
  `bulk_edit_channels`, который ставит один текст на все каналы аккаунта.

### Что осталось планом, а не сделано

* Массовая смена username у уже созданных объектов (Telegram ограничивает
  частоту; нужен отдельный режим с паузами).
* Назначение админов и закреп сообщений пакетно по проекту.
* Фильтры каталога в интерфейсе бота (сейчас фильтрация — через выгрузку CSV).
* Кнопка пакетного оформления в мини-аппе (API `POST
  /api/miniapp/global_presence/{plan_id}/bulk_apply` готов, экран — нет).
