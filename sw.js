// v3 - multi-set tracking, feedback, exercise info
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))));
  return self.clients.claim();
});
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
