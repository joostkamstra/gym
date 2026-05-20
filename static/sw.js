// v5 - ARE sunset cleanup: hydratie weg, PTR alle tabs, voeding-card boven, trends-target snapshot
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))));
  return self.clients.claim();
});
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
