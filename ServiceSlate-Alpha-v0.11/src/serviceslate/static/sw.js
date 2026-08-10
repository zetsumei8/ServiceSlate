const CACHE = 'serviceslate-shell-v9';
const SHELL = ['/', '/static/styles.css', '/static/app.js', '/static/manifest.webmanifest', '/static/brand-icon-192.png', '/static/brand-icon-512.png'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('activate', event => event.waitUntil(Promise.all([caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))), self.clients.claim()])));
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(event.request, copy));
    return r;
  }).catch(() => caches.match(event.request).then(r => r || caches.match('/'))));
});
