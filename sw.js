/* Atelier Coworking — service worker (PWA admin) */
const CACHE = 'acw-admin-v1';
const SHELL = [
  '/admin-devis-dashboard.html',
  '/admin-hub.html',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png',
  '/manifest.webmanifest'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Données live (API) : toujours le réseau, jamais de cache.
  if (url.pathname.startsWith('/api/')) return;

  // Le reste : réseau d'abord, cache en secours hors-ligne.
  e.respondWith(
    fetch(req)
      .then(res => {
        if (res && res.ok && url.origin === location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then(r => r || caches.match('/admin-devis-dashboard.html')))
  );
});
