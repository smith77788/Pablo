> **ЗАМОРОЖЕН.** Не добавляйте сюда новую функциональность. Это третья
> реализация того же продукта (TS + gramjs), в деплой она не входит: корневой
> `Dockerfile` собирает `tg-manager/`. Предметная область ниже — узлы и
> подузлы-топики — переносится в `tg-manager` как обычный тип операции; второй
> рантайм на другом языке ради одной доменной модели не оправдан.
> Решение и обоснование: [`../docs/adr/0001-one-kernel.md`](../docs/adr/0001-one-kernel.md).
>
> Текст ниже сохранён: он остаётся точным описанием того, как эти операции
> ложатся на реальные конструкторы MTProto, и это ровно та часть, которая
> переезжает.

# Infragram — Telegram Nodes Service

Автоматизация «узлов» (Node) и подузлов (SubNode) через MTProto (gramjs).
На реальной архитектуре Telegram, без выдуманных API:

| Домен       | Реальный конструктор MTProto                          |
| ----------- | ----------------------------------------------------- |
| Node        | `channels.CreateChannel(megagroup:true)` + `channels.ToggleForum` |
| Admin-титул | `channels.EditAdmin(rank)` — видимый бейдж            |
| SubNode     | `channels.CreateForumTopic` (id топика = `message_thread_id`) |
| Broadcast   | `messages.SendMessage(replyTo=topicId)`               |
| Invite      | `messages.ExportChatInvite`                           |
| Teardown    | `channels.DeleteChannel`                              |

## Осознанные границы реализации

Эти три решения зашиты в код намеренно и помечены комментариями:

1. **Авторизация — только StringSession** (штатный логин в СВОЙ аккаунт).
   Парсинг сырых `tdata` auth-key буферов в пул не реализован.
2. **Admin-титул ≠ анонимизация.** `channels.EditAdmin` ставит *видимый*
   кастомный титул (`rank`, ≤16 симв.). `anonymous` жёстко `false` — атрибуция
   действий остаётся прозрачной.
3. **Flood-wait уважается, а не обходится.** `withFloodRetry` ЖДЁТ ровно
   столько, сколько просит Telegram (`FLOOD_WAIT_x`). Троттлинг 3–5с на аккаунт
   и сериализация операций — чтобы самим не спровоцировать лимит.

## Архитектура

```
NodeDashboardController (REST)
        │
        ├── AccountProvider   → StringSession из пула (расшифровка через decrypt)
        ├── NodeRepository    → Prisma (Postgres)
        └── TelegramNodeEngine
                 └── NodeClientFactory  → gramjs client (connect→RPC→disconnect,
                                          mutex+throttle на аккаунт)
```

## Маршруты

| Метод + путь                    | Действие                                   |
| ------------------------------- | ------------------------------------------ |
| `POST /api/nodes/create`        | Провижининг: канал+форум → топики → ACTIVE |
| `POST /api/nodes/:id/toggle`    | ACTIVE ⇄ PAUSED                            |
| `DELETE /api/nodes/:id`         | DeleteChannel + каскадная очистка БД        |
| `POST /api/nodes/:id/broadcast` | Рассылка во все подузлы по `message_thread_id` |
| `POST /api/nodes/:id/invite`    | Экспорт/ротация инвайт-ссылки              |
| `GET /api/nodes/:id/logs`       | История активности                         |

## Запуск

```bash
npm install
export DATABASE_URL=postgres://user:pass@host:5432/infragram
export TG_API_ID=... TG_API_HASH=...      # fallback, если нет на уровне аккаунта
npx prisma migrate deploy
npm run build && npm start
```

> Статус верификации: код написан под gramjs `telegram@^2.22`, но сборка
> (`npm run build`) в этой среде не запускалась (нет доступа к npm-реестру).
> Перед деплоем выполните `npm install && npm run typecheck`.
