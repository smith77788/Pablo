/* Service worker Infragram — минимальный, ради установки как приложение и
 * работы оболочки офлайн. Стратегия: сеть-первым (мини-апп деплоится часто —
 * показывать свежий интерфейс важнее), кэш — только запасной аэродром офлайн.
 *
 * Важно: запросы к API (/api/*) НЕ трогаем — они и так вне scope (/miniapp/),
 * но на всякий случай пропускаем их мимо кэша, чтобы данные всегда были живыми.
 */
const CACHE = 'infragram-shell-v1';
const SHELL = ['.', 'manifest.webmanifest', 'telegram-web-app.js',
               'icon.svg', 'icon-maskable.svg'];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return Promise.all(SHELL.map(function (u) {
      return c.add(u).catch(function () {});   // один сбой не валит установку
    }));
  }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  const req = e.request;
  if (req.method !== 'GET') return;                 // мутации — только по сети
  const url = new URL(req.url);
  if (url.pathname.indexOf('/api/') !== -1) return;  // API — всегда живьём

  e.respondWith(
    fetch(req).then(function (resp) {
      // Кладём свежую копию оболочки в кэш (для офлайна).
      if (resp && resp.status === 200 && resp.type === 'basic') {
        const copy = resp.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy).catch(function(){}); });
      }
      return resp;
    }).catch(function () {
      // Офлайн: отдаём из кэша; для навигации — оболочку приложения.
      return caches.match(req).then(function (hit) {
        return hit || caches.match('.');
      });
    })
  );
});
