/* GoodMarket PWA service worker
 *
 * Strategy:
 *  - Precache a minimal shell (offline page + icons + manifest).
 *  - Static assets under /static/ are served cache-first (stale-while-revalidate).
 *  - Navigation requests are network-first with an offline fallback.
 *  - API / auth / blockchain / analytics requests bypass the cache entirely.
 *
 * Bump CACHE_VERSION any time the cached shell changes so clients pick it up.
 */

const CACHE_VERSION = 'v1-2026-04-21';
const STATIC_CACHE = `gm-static-${CACHE_VERSION}`;
const RUNTIME_CACHE = `gm-runtime-${CACHE_VERSION}`;

const PRECACHE_URLS = [
  '/static/manifest.json',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/offline.html',
];

// Never touch these — always go to the network, never cache.
const NETWORK_ONLY_PATTERNS = [
  /^\/api\//,
  /^\/auth\//,
  /^\/login/,
  /^\/logout/,
  /^\/admin/,
  /^\/claim/,
  /^\/webhook/,
  /^\/healthz?$/,
  /^\/__/,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .catch((err) => {
        // A single missing precache entry shouldn't block install.
        console.warn('[SW] Precache failed:', err);
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key !== STATIC_CACHE && key !== RUNTIME_CACHE)
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();

      // Let open tabs know a new SW is active so they can prompt the user.
      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((client) => client.postMessage({ type: 'SW_UPDATED' }));
    })()
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

function isNetworkOnly(url) {
  if (url.origin !== self.location.origin) return true; // cross-origin → don't cache
  return NETWORK_ONLY_PATTERNS.some((pattern) => pattern.test(url.pathname));
}

function isStaticAsset(url) {
  return (
    url.origin === self.location.origin &&
    (url.pathname.startsWith('/static/') || url.pathname === '/favicon.ico')
  );
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  const networkFetch = fetch(request)
    .then((response) => {
      if (response && response.status === 200 && response.type === 'basic') {
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    })
    .catch(() => cached);
  // `networkFetch` may resolve to `undefined` if the network fails and there
  // is no cached copy. respondWith() requires a Response, so fall back to a
  // minimal 503 instead of letting `undefined` bubble up.
  const response = cached || (await networkFetch);
  return (
    response ||
    new Response('Offline', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    })
  );
}

async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response && response.status === 200) {
      const cache = await caches.open(RUNTIME_CACHE);
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const offline = await caches.match('/static/offline.html');
    if (offline) return offline;
    return new Response('You are offline.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  if (isNetworkOnly(url)) return;

  if (request.mode === 'navigate') {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
});
