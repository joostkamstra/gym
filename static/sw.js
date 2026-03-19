// v4 - cloud version with caching + offline support
const CACHE_NAME = 'gym-v4';
const STATIC_ASSETS = ['/', '/index.html', '/manifest.json'];
const API_CACHE = 'gym-api-v4';

// Install: cache static assets
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// Activate: clean old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME && k !== API_CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch: cache-first for static, stale-while-revalidate for API GETs, network-only for API writes
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API GET requests: stale-while-revalidate
  if (url.pathname.startsWith('/api/') && e.request.method === 'GET') {
    e.respondWith(
      caches.open(API_CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          const fetchPromise = fetch(e.request).then(response => {
            if (response.ok) {
              cache.put(e.request, response.clone());
            }
            return response;
          }).catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // API write requests (POST/PUT/DELETE): network only, queue if offline
  if (url.pathname.startsWith('/api/') && e.request.method !== 'GET') {
    e.respondWith(fetch(e.request));
    return;
  }

  // Static assets: cache-first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});

// Background sync for offline workout submissions
self.addEventListener('sync', e => {
  if (e.tag === 'sync-workouts') {
    e.waitUntil(syncWorkouts());
  }
});

async function syncWorkouts() {
  // IndexedDB queue processing will be handled by the frontend
  // This is a placeholder for the Background Sync API integration
  const clients = await self.clients.matchAll();
  clients.forEach(client => client.postMessage({ type: 'SYNC_COMPLETE' }));
}
