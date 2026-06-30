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
