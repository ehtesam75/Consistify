{% load static %}
const CACHE_NAME = "consistify-static-v1";
const OFFLINE_URL = "{% url 'habits:index' %}";

const PRECACHE_URLS = [
  OFFLINE_URL,
  "{% static 'habits/css/styles.css' %}",
  "{% static 'habits/js/ui.js' %}",
  "{% static 'habits/js/charts.js' %}",
  "{% static 'habits/js/pwa-register.js' %}",
  "{% static 'habits/img/favicon.svg' %}",
  "{% static 'habits/img/icons/Consistify 192 x 192.png' %}",
  "{% static 'habits/img/icons/Consistify 512 x 512.png' %}",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) {
    return cached;
  }

  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }
    return (await caches.match(OFFLINE_URL)) || Response.error();
  }
}
