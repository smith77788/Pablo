const CACHE_NAME = 'nm-v3';
const PRECACHE_URLS = [
  '/',
  '/index.html',
  '/catalog.html',
  '/pricing.html',
  '/about.html',
  '/booking.html',
  '/cabinet.html',
  '/cases.html',
  '/reviews.html',
  '/faq.html',
  '/contact.html',
  '/favorites.html',
  '/order-status.html',
  '/offline.html',
  '/css/main.css',
  '/js/analytics.js',
  '/js/darkmode.js',
  '/js/booking.js'
];

// Install: precache static assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// Activate: clean up old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin and uploads
  if (url.origin !== location.origin) return;

  // Images from /uploads/ — CacheFirst with 7-day TTL
  if (url.pathname.startsWith('/uploads/')) {
    event.respondWith(cacheFirstWithTTL(request, 7 * 24 * 60 * 60 * 1000));
    return;
  }

  // Static assets (CSS, JS, fonts, SVG, icons) — CacheFirst
  if (
    url.pathname.match(/\.(css|js|woff2?|ttf|otf|svg|png|jpg|jpeg|webp|ico)$/)
  ) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // HTML pages and API — NetworkFirst
  if (
    request.headers.get('accept')?.includes('text/html') ||
    url.pathname.startsWith('/api/')
  ) {
    event.respondWith(networkFirst(request));
    return;
  }

  // Default — NetworkFirst
  event.respondWith(networkFirst(request));
});

// CacheFirst strategy
async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return offlineFallback(request);
  }
}

// CacheFirst with TTL for images
async function cacheFirstWithTTL(request, ttlMs) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) {
    const dateHeader = cached.headers.get('sw-cached-date');
    if (dateHeader) {
      const age = Date.now() - parseInt(dateHeader, 10);
      if (age < ttlMs) return cached;
    } else {
      return cached;
    }
  }
  try {
    const response = await fetch(request);
    if (response.ok) {
      // Clone with added timestamp header
      const headers = new Headers(response.headers);
      headers.set('sw-cached-date', String(Date.now()));
      const timestamped = new Response(await response.blob(), {
        status: response.status,
        statusText: response.statusText,
        headers
      });
      cache.put(request, timestamped);
    }
    return response;
  } catch {
    if (cached) return cached;
    return offlineFallback(request);
  }
}

// NetworkFirst strategy
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok && request.method === 'GET') {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return offlineFallback(request);
  }
}

// Offline fallback
async function offlineFallback(request) {
  if (request.headers.get('accept')?.includes('text/html')) {
    const cached = await caches.match('/offline.html');
    if (cached) return cached;
  }
  return new Response('Нет подключения к сети', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' }
  });
}
