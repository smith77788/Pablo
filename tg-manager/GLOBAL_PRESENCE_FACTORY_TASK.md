# GLOBAL PRESENCE FACTORY — IMPLEMENTATION TASK

You are working on the existing BotMother project.

Your task is to implement the Global Presence Factory.

Do NOT rebuild the project.
Do NOT create chaotic menus.
Do NOT create fake features.
Preserve existing architecture and reuse existing Operation Engine, Factory, Template, Targeting, Account Pool, Report, and Telegram UI systems wherever possible.

---

## 1. Product Goal

The user must be able to create Telegram presence across the world in a few guided clicks.

The flow should allow the user to:

1. Open the correct menu.
2. Choose what to create:
   - channels
   - groups/chats
   - bots
   - mixed presence package
3. Choose or create a template.
4. Set default title/name pattern.
5. Set default username pattern.
6. Select desired geography:
   - countries
   - cities
   - regions
   - worldwide presets
7. Select accounts/account pools for execution.
8. Preview the full creation plan.
9. Confirm the task.
10. Execute the global presence creation wave.
11. Track progress.
12. Retry failed items.
13. Receive a final report.

The user should feel:

> "I can deploy Telegram presence in every city in the world from one place."

---

## 2. Core Concept

This feature is a Factory + Operation Wave.

It must be implemented as:

```
Global Presence Factory
→ Global Presence Plan
→ Creation Wave
→ Operation Engine
→ Progress Tracking
→ Report
→ Retry Failed
```

Do NOT implement it as isolated button handlers.

---

## 3. User Flow

### Menu Entry

Add a clear Telegram-native menu entry: **🌍 Global Presence Factory**

The menu must explain simply:
> "This creates channels, chats, or bots for selected countries, regions, and cities using your template and accounts."

---

### Step 1 — Choose Asset Type

- Channels
- Groups / Chats
- Bots
- Full Presence Package (1 channel + 1 group + 1 bot per city)

If the current system cannot support all types yet, implement the supported types and clearly mark unsupported ones as planned.

---

### Step 2 — Choose Template

User can:
- choose existing template
- create a simple template
- continue without template if allowed

Template may include: title pattern, username pattern, description, avatar, first post, pinned post, rules, welcome message, bot commands, menu buttons, language, region placeholders.

Supported placeholders:
```
{{CITY}}
{{COUNTRY}}
{{REGION}}
{{LANGUAGE}}
{{COUNTRY_CODE}}
{{CITY_SLUG}}
{{COUNTRY_SLUG}}
{{INDEX}}
```

---

### Step 3 — Default Name Pattern

User sets display name/title pattern. Examples:
```
Crypto News {{CITY}}
AI Jobs {{CITY}}
Trading Community {{CITY}}
{{CITY}} Business Hub
```

System must show examples before confirmation.

---

### Step 4 — Default Username Pattern

User sets username pattern. Examples:
```
crypto_{{CITY_SLUG}}
ai_jobs_{{CITY_SLUG}}
trading_{{COUNTRY_CODE}}_{{CITY_SLUG}}
{{CITY_SLUG}}_business_hub
```

Username generation must support:
- slug generation & transliteration
- lowercasing, invalid character removal
- max length handling (max 32 chars for Telegram)
- collision detection + suffix variants
- fallback: `crypto_berlin` → `crypto_berlin_1` → `crypto_berlin_news` → `crypto_de_berlin`

---

### Step 5 — Geo Selection

Supported selection modes:
1. Countries
2. Regions
3. Cities
4. Worldwide presets
5. Custom imported geo list

Presets:
- Europe capitals, World capitals, Tier-1 global cities
- All cities in selected countries, Custom CSV/JSON list

Geo object:
```json
{
  "country": "Germany",
  "country_code": "de",
  "region": "Bavaria",
  "city": "Berlin",
  "city_slug": "berlin",
  "language": "de",
  "timezone": "Europe/Berlin",
  "priority": 1,
  "population": 3645000
}
```

---

### Step 6 — Account Selection

User selects: account, account pool, regional pool, auto-select best.

Considerations: health, permissions, current load, region/language tags, proxy region, previous failures, daily limits.

---

### Step 7 — Preview

Preview must include:
- asset type, # countries, # cities, # objects
- template, naming examples, username examples
- selected accounts/pools
- estimated duration, safety mode, risks

Example:
```
🌍 Global Presence Plan
────────────────────
Create: 120 channels
Across: 12 countries / 120 cities
Template: Crypto News City
Examples:
  Crypto News Berlin → @crypto_berlin
  Crypto News Paris  → @crypto_paris
Accounts: EU Pool (8 healthy)
Duration: ~4h 20m (safe mode)
```

Buttons: ✅ Confirm | ✏️ Edit Geo | 📋 Edit Template | 👤 Edit Accounts | ❌ Cancel

---

### Step 8 — Confirmation

Mass creation must never start without explicit confirmation.
Warn: "This will create Telegram infrastructure across selected geographies."

---

### Step 9 — Execution

Execution MUST run through Operation Engine / queue / worker.
Do NOT execute inside Telegram handlers directly.

Supports: queueing, safe pacing, staggered execution, account distribution, per-target status, progress updates, partial failures, retry failed, cancellation, report generation.

---

### Step 10 — Progress Tracking

```
🌍 Creating global presence...

Done:     42 / 120
✅ OK:    39
❌ Failed: 3
⏳ Current: Madrid

Estimated remaining: 2h 10m
```

Buttons: 🔄 Refresh | 🔁 Retry Failed | 📊 Report | ❌ Stop

---

### Step 11 — Final Report

Includes: total/created/failed/skipped, accounts used, geo coverage, created assets, failure reasons, retry classification, next actions.

Formats: Telegram summary + CSV/JSON export.

---

## 4. Data / Domain Requirements

### GlobalPresencePlan (DB table)
```sql
CREATE TABLE global_presence_plans (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    asset_type      TEXT NOT NULL,        -- 'channel'|'group'|'bot'|'package'
    template_id     INT,
    name_pattern    TEXT NOT NULL,
    username_pattern TEXT,
    geo_selection   JSONB NOT NULL DEFAULT '{}',
    account_selection JSONB NOT NULL DEFAULT '{}',
    safety_settings JSONB NOT NULL DEFAULT '{"safe_mode": true}',
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

### GlobalPresenceTarget (DB table)
```sql
CREATE TABLE global_presence_targets (
    id                  SERIAL PRIMARY KEY,
    plan_id             INT NOT NULL REFERENCES global_presence_plans(id) ON DELETE CASCADE,
    country             TEXT,
    country_code        TEXT,
    region              TEXT,
    city                TEXT,
    city_slug           TEXT,
    language            TEXT,
    timezone            TEXT,
    asset_type          TEXT NOT NULL,
    planned_name        TEXT,
    planned_username    TEXT,
    selected_account_id INT,
    status              TEXT NOT NULL DEFAULT 'pending',
    result_asset_id     BIGINT,
    error_message       TEXT,
    retryable           BOOLEAN DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT now()
);
```

---

## 5. Operation Types

Add to op_worker:
- `global_presence_channel` — create channel by geo target
- `global_presence_group` — create group by geo target
- `global_presence_bot` — create bot by geo target (v2)
- `global_presence_package` — full package (v2)

---

## 6. Safety Rules

- No execution without preview
- No execution without confirmation
- Conservative pacing by default (45-90s between creations)
- Account workload balancing
- Per-account limits
- Clear warnings for plans > 20 items
- Dry-run/preview mode
- Retry failed instead of repeating all
- Emergency stop integration

---

## 7. Telegram UX Rules

Guided flow:
```
🌍 Global Presence Factory
→ 1️⃣ Asset Type
→ 2️⃣ Template
→ 3️⃣ Name Pattern
→ 4️⃣ Username Pattern
→ 5️⃣ Geo Selection
→ 6️⃣ Accounts
→ 7️⃣ Preview
→ 8️⃣ Confirm
→ Progress
→ Report
```

Every screen: short title + explanation + current summary + Back + Cancel.
Pagination for: countries, cities, templates, accounts.
Search for: countries, cities, templates, accounts.

---

## 8. Geo UX

Presets for quick selection:
- 🌍 Europe capitals (44 cities)
- 🌎 World capitals (195 cities)
- 🏙️ Tier-1 global (50 cities)
- 🇩🇪 Germany + Austria + Switzerland
- 🌐 LATAM
- Custom import

---

## 9. Template Placeholder Engine

```python
def render_pattern(pattern: str, geo: dict) -> str:
    replacements = {
        "{{CITY}}": geo.get("city", ""),
        "{{COUNTRY}}": geo.get("country", ""),
        "{{REGION}}": geo.get("region", ""),
        "{{LANGUAGE}}": geo.get("language", ""),
        "{{COUNTRY_CODE}}": geo.get("country_code", "").upper(),
        "{{CITY_SLUG}}": geo.get("city_slug", ""),
        "{{COUNTRY_SLUG}}": geo.get("country_slug", geo.get("country_code", "")),
        "{{INDEX}}": str(geo.get("index", 1)),
    }
    for key, val in replacements.items():
        pattern = pattern.replace(key, val)
    return pattern
```

Missing placeholder values must NOT crash execution — replace with safe defaults.

---

## 10. Username Uniqueness Engine

```python
def generate_username_variants(base: str) -> list[str]:
    # base: "crypto_berlin"
    # returns: ["crypto_berlin", "crypto_berlin_1", "crypto_berlin_hub",
    #           "crypto_berlin_news", "crypto_de_berlin", ...]
```

Requirements:
- Max 32 chars, min 5 chars
- Only a-z, 0-9, underscore
- No consecutive underscores, no leading/trailing underscore
- Transliterate non-Latin (cyrillic → latin)
- Availability check via Telethon where supported
- Store attempted variants per target

---

## 11. Account Distribution

For each target, select account based on: pool → health → load → permissions → region tags → previous failures.

Basic v1: round-robin across selected accounts.
v2: weighted selection by health_score + load + region.

---

## 12. Failure Handling

Each failure must record: target, asset_type, account, reason, retryable, suggested_fix.

Failure types:
- `username_unavailable` → retryable (try next variant)
- `account_rate_limited` → retryable (wait)
- `account_banned` → non-retryable (switch account)
- `no_admin_rights` → non-retryable (fix manually)
- `proxy_error` → retryable
- `telegram_api_error` → depends on code
- `invalid_geo` → non-retryable

---

## 13. Implementation Status

### ✅ V1 Scope (implement now)
- [ ] Menu entry in BotMother → Operations
- [ ] FSM wizard: asset type → template → name pattern → username pattern → geo → accounts → preview → confirm
- [ ] Channels only (best supported asset type)
- [ ] Geo: presets + manual city list entry
- [ ] Template: reuse existing asset_templates system
- [ ] Placeholder engine: `render_pattern(pattern, geo_dict)`
- [ ] Username engine: slugify + variants + basic validation
- [ ] Account: round-robin from selected pool
- [ ] Preview screen with examples
- [ ] Confirmation gate
- [ ] op_worker: `global_presence_channel` operation type
- [ ] Progress tracking via operation_queue
- [ ] Final report
- [ ] DB: global_presence_plans + global_presence_targets

### 🔜 V2 Scope (document as planned)
- [ ] Groups/bots/packages
- [ ] Full geo database with 10k+ cities
- [ ] Weighted account distribution
- [ ] Availability check for usernames
- [ ] CSV/JSON geo import
- [ ] Avatar upload per geo
- [ ] First post per geo
- [ ] Mini App progress dashboard

---

## 14. Files to Create/Modify

### New files:
- `bot/handlers/global_presence.py` — FSM wizard + callbacks
- `services/geo_data.py` — geo seed data + presets
- `services/username_engine.py` — slug + uniqueness + variants
- `services/presence_planner.py` — plan builder + target generation
- `database/schema_v35.sql` — new tables

### Modify:
- `bot/callbacks.py` — add `GeoPresenceCb(prefix="gp")`
- `bot/states.py` — add `GlobalPresenceFSM`
- `database/db.py` — add plan/target CRUD functions
- `services/op_worker.py` — add `global_presence_channel` handler
- `bot/handlers/botmother_menu.py` — add menu entry
- `main.py` — register new router

---

## 15. Manual Test Scenario

```
User: Create channels for European capitals
Input:
  asset_type: channel
  template: Crypto News {{CITY}}
  username: crypto_{{CITY_SLUG}}
  geo: Europe capitals (44 cities)
  accounts: round-robin from 3 accounts
  safety: safe mode (60s between creations)

Expected:
  ✅ Preview shows 44 cities with names + usernames
  ✅ Confirmation required
  ✅ Operation queued (not inline)
  ✅ Progress visible
  ✅ Failures recorded per city
  ✅ Retry failed available
  ✅ Final report generated
  ✅ 44 channels created across EU capitals
```

---

## 16. Acceptance Criteria

- [ ] Clear Telegram menu entry exists
- [ ] Flow is guided (wizard), not chaotic
- [ ] Templates/placeholders work ({{CITY}} renders correctly)
- [ ] Geo selection works (at least presets)
- [ ] Username uniqueness has fallback logic
- [ ] Account selection exists (at least round-robin)
- [ ] Preview shown before execution
- [ ] Confirmation required
- [ ] Mass execution runs through op_worker, NOT inline
- [ ] Progress visible via operation_queue
- [ ] Failures recorded per target
- [ ] Retry failed exists
- [ ] Final report generated
- [ ] No fake features presented as complete

---

_Created: 2026-05-28_
_Status: IMPLEMENTATION PENDING_
_Priority: HIGH_
