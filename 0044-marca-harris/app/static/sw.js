// Service worker mínimo: solo cachea archivos estáticos (CSS, íconos) para
// que la app cargue más rápido y sea instalable. Las páginas del ERP son
// dinámicas (formularios con CSRF, sesión), así que a propósito NO se
// cachean — siempre se piden a la red para no romper el login ni los
// formularios con datos desactualizados.
// v2 (16 sep): los íconos cambiaron a la marca de Harris -- subir la
// versión fuerza que activate() borre el caché viejo con los íconos
// anteriores en vez de servirlos indefinidamente a quien ya tenía la PWA
// instalada.
const CACHE_NAME = "erp-transporte-static-v2";
const STATIC_ASSETS = [
  "/static/css/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
  "/static/img/harris-logo-lockup.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isStaticAsset = STATIC_ASSETS.some((path) => url.pathname === path);

  if (isStaticAsset) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
  // Todo lo demás (páginas dinámicas) se deja pasar directo a la red.
});
