'use strict';

const STATIC_CACHE = 'nevesty-static-v4';
const API_CACHE    = 'nevesty-api-v1';
const IMAGE_CACHE  = 'nevesty-images-v1';

// All current caches — anything else will be deleted on activate
const CURRENT_CACHES = [STATIC_CACHE, API_CACHE, IMAGE_CACHE, 'nm-pending-forms'];

const PRECACHE_URLS = [
  '/',
  '/index.html',
  '/catalog.html',
  '/model.html',
  '/booking.html',
  '/pricing.html',
  '/about.html',
  '/contact.html',
  '/reviews.html',
  '/404.html',
  '/offline.html',
  '/cabinet.html',
  '/cases.html',
  '/faq.html',
  '/favorites.html',
  '/order-status.html',
  '/compare.html',
  '/search.html',
  '/privacy.html',
  '/webapp.html',
  '/css/main.css',
  '/js/main.js',
  '/js/catalog.js',
  '/js/booking.js',
  '/js/analytics.js',
  '/js/darkmode.js',
  '/js/cookie-consent.js',
  '/manifest.json',
];

// ── Install: precache static assets ──────────────────────────────────────────
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache =>
      cache.addAll(
        PRECACHE_URLS.map(url => new Request(url, { credentials: 'same-origin' }))
      ).catch(err => console.warn('[SW] Precache partial failure:', err))
    )
  );
});

// ── Activate: delete stale caches ────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(names =>
      Promise.all(
        names
          .filter(name => !CURRENT_CACHES.includes(name))
          .map(name => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch routing ─────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin GET requests
  if (request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  // Uploads: CacheFirst with 7-day TTL
  if (url.pathname.startsWith('/uploads/')) {
    event.respondWith(cacheFirstWithTTL(request, IMAGE_CACHE, 7 * 24 * 60 * 60 * 1000));
    return;
  }

  // API: NetworkFirst with 5-second timeout, fallback to API_CACHE
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstWithTimeout(request, API_CACHE, 5000));
    return;
  }

  // Static assets (CSS, JS, fonts, images, icons): CacheFirst
  if (url.pathname.match(/\.(css|js|woff2?|ttf|otf|svg|png|jpg|jpeg|webp|ico)$/)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // HTML navigation and everything else: NetworkFirst
  event.respondWith(networkFirst(request, STATIC_CACHE));
});

// ── Strategy: CacheFirst ──────────────────────────────────────────────────────
async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return offlineFallback(request);
  }
}

// ── Strategy: CacheFirst with TTL (for /uploads/) ────────────────────────────
async function cacheFirstWithTTL(request, cacheName, ttlMs) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) {
    const dateHeader = cached.headers.get('sw-cached-date');
    if (!dateHeader || Date.now() - parseInt(dateHeader, 10) < ttlMs) {
      return cached;
    }
  }

  try {
    const response = await fetch(request);
    if (response.ok) {
      const headers = new Headers(response.headers);
      headers.set('sw-cached-date', String(Date.now()));
      const timestamped = new Response(await response.blob(), {
        status: response.status,
        statusText: response.statusText,
        headers,
      });
      cache.put(request, timestamped);
    }
    return response;
  } catch {
    if (cached) return cached;
    return offlineFallback(request);
  }
}

// ── Strategy: NetworkFirst ────────────────────────────────────────────────────
async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    return offlineFallback(request);
  }
}

// ── Strategy: NetworkFirst with timeout (for /api/) ───────────────────────────
async function networkFirstWithTimeout(request, cacheName, timeoutMs) {
  const cache = await caches.open(cacheName);

  const timeoutPromise = new Promise((_, reject) =>
    setTimeout(() => reject(new Error('SW timeout')), timeoutMs)
  );

  try {
    const response = await Promise.race([fetch(request), timeoutPromise]);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: 'Нет подключения к сети' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

// ── Offline fallback ──────────────────────────────────────────────────────────
async function offlineFallback(request) {
  const accept = request.headers.get('accept') || '';
  if (accept.includes('text/html') || request.mode === 'navigate') {
    const page = await caches.match('/offline.html');
    if (page) return page;
  }
  return new Response('Нет подключения к сети', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

// ── Background Sync — retry failed form submissions ───────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'nm-form-sync') {
    event.waitUntil(replayPendingForms());
  }
});

async function replayPendingForms() {
  let pending = [];
  try {
    const cache = await caches.open('nm-pending-forms');
    pending = await cache.keys();
  } catch { return; }

  for (const req of pending) {
    try {
      const resp = await fetch(req.clone());
      if (resp.ok) {
        const cache = await caches.open('nm-pending-forms');
        await cache.delete(req);
      }
    } catch {
      // Keep in queue — will retry on next sync event
    }
  }
}

// ── Push Notifications ────────────────────────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  let data = {};
  try {
    data = event.data.json();
  } catch {
    data = { title: 'Nevesty Models', body: event.data.text() };
  }

  const options = {
    body: data.body || 'У вас новое сообщение',
    icon: '/icons/icon-192.svg',
    badge: '/icons/icon-192.svg',
    tag: data.tag || 'nm-notification',
    data: { url: data.url || '/' },
    actions: data.actions || [],
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'Nevesty Models', options)
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
