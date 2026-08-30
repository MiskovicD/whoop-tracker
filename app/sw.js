/* Service worker voor Whoop lokaal.

   Strategie, en waarom:
   - De HTML gaat NETWERK-EERST. Cache-first op het document betekent dat de
     app altijd één versie achterloopt: je pusht een fix en je telefoon blijft
     de oude tonen. Netwerk-eerst met cache als terugval geeft je de nieuwste
     versie zodra je online bent, en werkt nog steeds offline.
   - Iconen en manifest gaan CACHE-EERST. Die veranderen zelden en zijn groot
     genoeg om niet elke keer opnieuw te willen halen.
   - Supabase komt hier niet langs. Verouderde gezondheidsdata tonen is erger
     dan even niets tonen.

   CACHE ophogen blijft nodig bij wijzigingen aan de statische bestanden. */
const CACHE = "whoop-lokaal-v11";
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

function isDocument(req) {
  return req.mode === "navigate" || (req.destination === "" && req.url.endsWith(".html"))
      || req.destination === "document";
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;         // Supabase nooit uit cache
  if (e.request.method !== "GET") return;

  if (isDocument(e.request)) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const kopie = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, kopie)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(e.request).then(hit => hit || caches.match("./index.html")))
    );
    return;
  }

  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
