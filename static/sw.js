// v5 - offline sync with IndexedDB queue
const CACHE_NAME = 'gym-v5';
const STATIC_ASSETS = ['/', '/index.html', '/manifest.json'];
const API_CACHE = 'gym-api-v5';
const DB_NAME = 'gym-offline';
const DB_VERSION = 1;
const STORE_NAME = 'workout-queue';

// === IndexedDB helpers ===
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function addToQueue(data) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).add(data);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function getAllQueued() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const req = tx.objectStore(STORE_NAME).getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function removeFromQueue(id) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// === Install: cache static assets ===
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// === Activate: clean old caches ===
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME && k !== API_CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// === Fetch: handle API requests with offline fallback ===
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API POST /api/workouts: try network, queue if offline
  if (url.pathname === '/api/workouts' && e.request.method === 'POST') {
    e.respondWith(handleWorkoutPost(e.request));
    return;
  }

  // API GET requests: stale-while-revalidate
  if (url.pathname.startsWith('/api/') && e.request.method === 'GET') {
    e.respondWith(
      caches.open(API_CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          const fetchPromise = fetch(e.request).then(response => {
            if (response.ok) cache.put(e.request, response.clone());
            return response;
          }).catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // Other API writes: network only
  if (url.pathname.startsWith('/api/') && e.request.method !== 'GET') {
    e.respondWith(fetch(e.request));
    return;
  }

  // Static assets: cache-first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});

// Handle workout POST with offline queue
async function handleWorkoutPost(request) {
  try {
    const response = await fetch(request.clone());
    if (response.ok) return response;
    throw new Error('Server error');
  } catch (err) {
    // Offline or server error: queue the workout
    const body = await request.clone().json();
    const token = request.headers.get('Authorization');
    await addToQueue({ body, token, timestamp: Date.now() });

    // Notify the app
    const clients = await self.clients.matchAll();
    clients.forEach(c => c.postMessage({ type: 'WORKOUT_QUEUED', count: 1 }));

    // Return a synthetic response so the app knows it's queued
    return new Response(JSON.stringify({
      id: 'offline-' + Date.now(),
      queued: true,
      message: 'Workout opgeslagen in offline queue'
    }), {
      status: 202,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// === Background Sync ===
self.addEventListener('sync', e => {
  if (e.tag === 'sync-workouts') {
    e.waitUntil(syncWorkouts());
  }
});

async function syncWorkouts() {
  const queued = await getAllQueued();
  if (!queued.length) return;

  let synced = 0;
  for (const item of queued) {
    try {
      const response = await fetch('/api/workouts', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': item.token
        },
        body: JSON.stringify(item.body)
      });
      if (response.ok) {
        await removeFromQueue(item.id);
        synced++;
      }
    } catch (err) {
      // Still offline, will retry on next sync
      break;
    }
  }

  if (synced > 0) {
    const clients = await self.clients.matchAll();
    clients.forEach(c => c.postMessage({ type: 'SYNC_COMPLETE', synced }));
  }
}

// === Periodic check (fallback for browsers without Background Sync) ===
self.addEventListener('message', e => {
  if (e.data === 'SYNC_NOW') {
    syncWorkouts();
  }
});
