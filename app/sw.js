// Kvitt service worker: offline shell + push notifications
const CACHE = "backtrack-v5";
const SHELL = ["./", "index.html", "manifest.json", "icons/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  // network first, fall back to cache when offline
  e.respondWith(fetch(e.request, { cache: "no-cache" }).then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; }).catch(() => caches.match(e.request)));
});
async function idFor(endpoint) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(h)].slice(0, 12).map(b => b.toString(16).padStart(2, "0")).join("");
}
self.addEventListener("push", e => {
  e.waitUntil((async () => {
    let title = "Backtrack", body = "Open Backtrack for the latest train info.";
    try {
      const cfg = await fetch("config.json", { cache: "no-store" }).then(r => r.json());
      const sub = await self.registration.pushManager.getSubscription();
      if (cfg.server && sub) {
        const m = await fetch(cfg.server.replace(/\/$/, "") + "/msg?id=" + await idFor(sub.endpoint), { cache: "no-store" }).then(r => r.json());
        title = m.title || title; body = m.body || body;
      }
    } catch (_) {}
    await self.registration.showNotification(title, { body, icon: "icons/icon-192.png", badge: "icons/icon-192.png", tag: "kvitt" });
  })());
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window" }).then(ws => ws.length ? ws[0].focus() : clients.openWindow("./")));
});
