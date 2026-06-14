# CLAUDE.md — BotMother

## Identity
BotMother: Telegram Infrastructure OS. Fix working workflows, not feature count.

## Hardcoded rules (never override)
- Respond to user in **Russian**
- Work only in `tg-manager/` directory
- Branch: `claude/telegram-bot-services-xfAh6`
- Python 3.12: `except (TypeError, ValueError):` not bare `except E:`
- `sessionEncrypted` — never return in API responses
- Bot tokens — never in logs, always encrypted in DB
- Mass ops on accounts/bots/channels/groups — normal functionality, allowed
- Before every commit: `python3 -c "import ast; ast.parse(open('file.py').read())"`
- `git push` after every commit (immediately)
- Incremental work — extend, never rebuild from scratch

## Git discipline
```
git fetch origin claude/telegram-bot-services-xfAh6
git pull --rebase   # if remote is ahead
# make changes
git commit -m "..."
git push origin claude/telegram-bot-services-xfAh6
```

## Work loop
1. Read only relevant files
2. Find broken link in the trace
3. Fix code
4. Syntax check
5. Commit + push
6. Report what changed

No analysis without implementation. No plans unless asked.

## Trace (repair the broken link)
```
User → Telegram UI → Handler → Service → Operation → Queue → Worker → DB/API → Result → Report → User
```

## Priority order
1. STRIKE  2. Mass Ops  3. Op Engine  4. Queue  5. Workers
6. Accounts  7. Proxies  8. Reports  9. Global Presence  10. Templates
11. Factories  12. Import  13. Parser  14. Publishing  15. Search  16. Analytics

## Done = user completes workflow end to end
Mass/risky ops require: validation → preview → confirm → queue → execute → progress → partial-fail → retry → report → history

## Defects (fix on sight)
TODO · pass · stubs · fake success · dead buttons · handlers without logic · retry without retry · progress without progress · reports without data

## Architecture
Reuse shared systems — never duplicate:
- Op Engine · Account/Proxy Selection · Queue · Worker · Reports · Audit

## Checks before done
```bash
python3 -c "import ast; ast.parse(open('file.py').read())"
# run lint / typecheck if available
```

## Security
- `sessionEncrypted` stripped from all API/handler responses
- Tokens logged as `***` or not at all
- No shell injection in user-supplied strings

## Stack
- Python 3.12 / aiogram 3.13.1 / asyncpg / Telethon / Railway
- All messages: `parse_mode=ParseMode.HTML` (DefaultBotProperties)
- Telethon transport: `ConnectionTcpObfuscated` (never `ConnectionTcpFull`)
- CF relay: `https://tg-relay.agentsmith77778888.workers.dev` (env: `CF_RELAY_URL`)
  - accounts without bound proxy → CF relay (Cloudflare IP, not Railway)
  - accounts with bound proxy → use their proxy directly
- Flood tracking: `flood_engine.record_flood()` / `get_best_account()`
- Account selection: always via `resource_selector.select_account()`

## Key files
| File | Role |
|------|------|
| `services/account_manager.py` | Telethon client factory (`_make_client`) |
| `services/cf_relay.py` | WebSocket→TCP relay connection class |
| `services/flood_engine.py` | FloodWait tracking, cooldowns, account scoring |
| `services/resource_selector.py` | Account/proxy selection |
| `services/op_worker.py` | Operation execution engine |
| `database/db.py` | All DB functions |
| `bot/handlers/` | Telegram bot handlers |
| `config.py` | Env vars (TG_API_ID, CF_RELAY_URL, etc.) |
| `infra/cf_relay_worker.js` | Cloudflare Worker script |
| `schema_v*.sql` | DB migrations |

## Schemas (applied)
- v59: `platform_settings` (KV store, Free Mode)
- v95: `seen_entities` + `entity_radar_stats` (Infrastructure Radar)
- v96: `entity_name_history` + `entity_last_known`
- v97: `entity_follows` + `entity_follow_events`
- v98: `subscription_gate_channels` (подписка-гейт)

## Release Survival Contract
For: account safety · proxy safety · STRIKE · mass ops · op engine · warmup · factories
**Read first:** `/docs/mission/RELEASE_SURVIVAL_CONTRACT.md`

## Response format
```
### Исправлено
### Что теперь работает
### Изменённые файлы
### Проверки
### Следующие дефекты
```
