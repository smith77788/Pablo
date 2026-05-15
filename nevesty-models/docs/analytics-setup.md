# Analytics Setup

## Configuration

Analytics IDs are stored in the admin panel (Settings → Analytics) and loaded at runtime via `/api/settings/public`. There are **no hardcoded IDs** in HTML files — set them through the admin panel.

## Google Analytics 4

1. Go to [analytics.google.com](https://analytics.google.com) → Admin → Data Streams → Web
2. Copy the **Measurement ID** (format: `G-XXXXXXXXXX`)
3. In the admin panel: Settings → Analytics → GA4 Measurement ID → paste the ID

## Yandex.Metrica

1. Go to [metrica.yandex.ru](https://metrica.yandex.ru) → your counter → Settings
2. Copy the **Counter number** (numeric, e.g. `12345678`)
3. In the admin panel: Settings → Analytics → Yandex.Metrica Counter ID → paste the ID

## Events tracked

| Event name | Trigger | GA4 | Yandex.Metrica |
|---|---|---|---|
| `view_model` | User opens a model card | `select_item` + `view_model` | `view_model` |
| `begin_checkout` | User clicks booking button | `begin_checkout` | `begin_booking` |
| `purchase` | Order successfully submitted | `purchase` | `order_submitted` |
| `add_to_wishlist` | Model added to favorites | `add_to_wishlist` | `add_to_wishlist` |
| `add_to_compare` | Model added to compare | `add_to_compare` | `add_to_compare` |
| `search` | Search/filter applied in catalog | `search` | `search` |
| `filter_applied` | Filter applied in catalog | `filter_applied` | `filter_catalog` |
| `contact_whatsapp` | WhatsApp link clicked | `contact_whatsapp` | `contact_whatsapp` |
| `contact_telegram` | Telegram link clicked | `contact_telegram` | `contact_telegram` |
| `page_view` | Page loaded (with UTM params) | `page_view` | — |

## Consent management

Analytics scripts load **only after cookie consent is accepted** (`cookie_consent === 'accepted'` in `localStorage`). The cookie banner is shown to new visitors on the first page load.

Late consent is also supported: if the user accepts cookies after page load, analytics scripts are loaded immediately via the `cookieConsentAccepted` event.

## UTM parameters

UTM parameters (`utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`) are automatically extracted from the URL and attached to all conversion events (`view_model`, `begin_checkout`, `purchase`). They are persisted in `sessionStorage` for cross-page attribution.

## Implementation files

- `public/js/analytics.js` — main analytics wrapper (`NM.analytics`)
- `public/js/booking.js` — fires `purchase` event on order completion
- `public/js/catalog.js` — fires filter/search/view events in catalog
- `public/js/main.js` — fires `view_model` and `begin_checkout` in model modal
