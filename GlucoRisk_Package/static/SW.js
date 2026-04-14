// sw.js — GlucoRisk Service Worker
// Must be served from root: /sw.js

self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());

// Listen for push events sent from the page via postMessage
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
    const { title, body, icon, badge, tag, data } = event.data.payload;
    self.registration.showNotification(title, {
      body,
      icon:  icon  || '/static/icon.png',
      badge: badge || '/static/icon.png',
      tag:   tag   || 'glucorisk-alert',
      vibrate: [200, 100, 200, 100, 400],
      requireInteraction: data?.risk === 'HIGH_RISK',
      data: data || {},
      actions: [
        { action: 'view',    title: '📊 View Dashboard' },
        { action: 'dismiss', title: '✕ Dismiss' }
      ]
    });
  }
});

// Handle notification click
self.addEventListener('notificationclick', event => {
  event.notification.close();
  if (event.action === 'dismiss') return;
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes('/dashboard') && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow('/dashboard');
    })
  );
});