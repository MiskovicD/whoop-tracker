/* Service worker voor Whoop lokaal.
   Cachet alleen de app-bestanden zelf, zodat hij opent zonder internet.
   Je metrics komen live uit Supabase en worden bewust niet gecachet -
   verouderde gezondheidsdata tonen is erger dan even niets tonen.

   Let op: verhoog CACHE bij elke wijziging, anders blijft de oude versie hangen. */
const CACHE = "whoop-lokaal-v4";
const ASSETS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
  "./favicon-32.png",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // Supabase nooit uit cache
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request))
  );
});
