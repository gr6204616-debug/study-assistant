// عامل الخدمة: يستقبل إشعارات الجوال حتى لو كان التطبيق مغلقاً،
// ويحفظ نسخة من الصفحة لتفتح بدون إنترنت.
const CACHE = 'study-v1';

self.addEventListener('install', event => {
    event.waitUntil(caches.open(CACHE).then(c => c.addAll(['/', '/icons/icon-192.png'])).catch(() => {}));
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

// الصفحة من الإنترنت أولاً، وعند انقطاعه نعرض آخر نسخة محفوظة
self.addEventListener('fetch', event => {
    if (event.request.mode !== 'navigate') return;
    event.respondWith(
        fetch(event.request)
            .then(res => {
                const copy = res.clone();
                caches.open(CACHE).then(c => c.put('/', copy));
                return res;
            })
            .catch(() => caches.match('/'))
    );
});

self.addEventListener('push', event => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
    const title = data.title || 'مساعد دراسي';
    event.waitUntil(self.registration.showNotification(title, {
        body: data.body || 'يوجد محتوى جديد',
        icon: '/icons/icon-192.png',
        badge: '/icons/badge-96.png',
        tag: data.tag || 'study',
        renotify: true,
        vibrate: [200, 100, 200],
        dir: 'rtl',
        lang: 'ar',
        data: { url: data.url || '/' },
    }));
});

// عند الضغط على الإشعار: نفتح التطبيق على المادة أو التبليغ المقصود
self.addEventListener('notificationclick', event => {
    event.notification.close();
    const target = new URL(event.notification.data && event.notification.data.url || '/', self.location.origin);
    event.waitUntil((async () => {
        const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        for (const w of wins) {
            if (new URL(w.url).origin === target.origin) {
                w.postMessage({ type: 'navigate', hash: target.hash });
                return w.focus();
            }
        }
        return self.clients.openWindow(target.href);
    })());
});
