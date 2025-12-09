self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open("gems-app-v1").then((cache) =>
      cache.addAll([
        "/static/app/index.html",
        "/static/app/app.js",
        "/static/app/manifest.json"
      ])
    )
  );
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    caches.match(event.request).then((res) => res || fetch(event.request))
  );
});
