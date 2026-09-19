/* evprices — service worker.
   Páginas: rede primeiro, cai para a última cópia em cache (ou /offline) sem conexão.
   /static: rede primeiro (as URLs levam ?v=hash, então uma versão nova nunca fica presa no cache); cai para o cache
   sem conexão. /api e POST: nunca em cache (dados vivos). */
const VERSION = 'evprices-v18';
const PRECACHE = ['/offline', '/static/pwa/manifest.webmanifest', '/static/pwa/icon-192.png', '/static/pwa/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;

  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then(hit => hit || caches.match('/offline'))));
});
