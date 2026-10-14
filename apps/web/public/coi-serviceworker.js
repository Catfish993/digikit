/*! coi-serviceworker, after gzuidhof/coi-serviceworker (MIT). GitHub Pages cannot send the
 * Cross-Origin-Opener-Policy / Cross-Origin-Embedder-Policy headers that SharedArrayBuffer needs,
 * so this worker adds them to every response of the page's origin. The first visit registers it
 * and reloads once; after that `crossOriginIsolated` is true. No firmware or derived data is here.
 */
if (typeof window === 'undefined') {
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
  self.addEventListener('fetch', (event) => {
    const r = event.request;
    if (r.cache === 'only-if-cached' && r.mode !== 'same-origin') return;
    event.respondWith(
      fetch(r)
        .then((response) => {
          if (response.status === 0) return response;
          const headers = new Headers(response.headers);
          headers.set('Cross-Origin-Embedder-Policy', 'require-corp');
          headers.set('Cross-Origin-Opener-Policy', 'same-origin');
          headers.set('Cross-Origin-Resource-Policy', 'same-origin');
          return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
        })
        .catch((error) => console.error(error)),
    );
  });
} else {
  (() => {
    const reload = () => window.location.reload();
    const isolated = window.crossOriginIsolated;
    const n = navigator;
    if (isolated || !window.isSecureContext || !n.serviceWorker) return;
    // A page that already reloaded once and is still not isolated: give up (single-thread fallback).
    if (sessionStorage.getItem('coi-reloaded')) return;
    n.serviceWorker.register(window.document.currentScript.src).then(
      (registration) => {
        registration.addEventListener('updatefound', reload);
        if (registration.active && !n.serviceWorker.controller) {
          sessionStorage.setItem('coi-reloaded', '1');
          reload();
        }
      },
      (error) => console.error('coi-serviceworker registration failed:', error),
    );
  })();
}
