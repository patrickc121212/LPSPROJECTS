/* Live map + SSE client for Dad's Tesla Tracker.
 *
 * - Subscribes to /stream and reacts to three event types:
 *     'vehicles' → {vehicle_key, latitude, longitude, speed_mph, battery_pct, online}
 *     'doors'    → [{door_key, is_open, updated_at}, ...]
 *     'inbox'    → {recipient_key}
 * - Vehicle data may arrive partially (some vehicles have no fix yet),
 *   so we merge into a client-side state map.
 */
(function () {
  'use strict';

  const vehicleByKey = Object.fromEntries(VEHICLES.map(v => [v.key, v]));
  const doorByKey    = Object.fromEntries(DOORS.map(d => [d.key, d]));

  // Map --------------------------------------------------------------
  const initialCenter = DOORS[0] ? [DOORS[0].latitude, DOORS[0].longitude] : [37.7749, -122.4194];
  const map = L.map('map').setView(initialCenter, 16);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© OpenStreetMap contributors'
  }).addTo(map);

  // Geofence circles
  DOORS.forEach(d => {
    L.circle([d.latitude, d.longitude], {
      radius: d.radius_m,
      color: '#888',
      weight: 1,
      fillOpacity: 0.08,
    }).addTo(map).bindPopup(`<strong>${d.label}</strong><br>owner: ${vehicleByKey[d.owner_key]?.label}`);
  });

  // Vehicle markers
  const markers = {};
  VEHICLES.forEach(v => {
    const icon = L.divIcon({
      className: 'tesla-marker',
      html: `<div style="background:${v.color}">${v.label.split(' ')[0]}</div>`,
      iconSize: [40, 40],
      iconAnchor: [20, 20],
    });
    const m = L.marker(initialCenter, {icon}).addTo(map);
    m.bindPopup(v.label);
    markers[v.key] = m;
  });

  // State -------------------------------------------------------------
  const state = {};
  VEHICLES.forEach(v => state[v.key] = {vehicle_key: v.key, online: false});

  function applyVehicles(rows) {
    // `rows` is a full snapshot from /api/vehicles OR an SSE payload.
    const list = Array.isArray(rows) ? rows : Object.values(rows);
    list.forEach(row => {
      const k = row.vehicle_key;
      if (!k || !markers[k]) return;
      Object.assign(state[k], row);

      const meta = document.getElementById('meta-' + k);
      if (meta) {
        const pct = (row.battery_pct != null) ? `${row.battery_pct}%` : '—';
        const mph = (row.speed_mph != null)  ? `${Math.round(row.speed_mph)} mph` : '—';
        const online = row.online ? '🟢' : '⚪';
        meta.textContent = `${online} ${pct} · ${mph}`;
      }

      if (row.latitude != null && row.longitude != null) {
        markers[k].setLatLng([row.latitude, row.longitude]);
      }
    });
  }

  function applyDoors(rows) {
    rows.forEach(row => {
      const el = document.getElementById('door-state-' + row.door_key);
      if (!el) return;
      if (row.is_open === null || row.is_open === undefined) {
        el.textContent = 'unknown';
        el.className = 'state';
      } else {
        el.textContent = row.is_open ? 'OPEN' : 'closed';
        el.className = 'state ' + (row.is_open ? 'open' : 'closed');
      }
    });
  }

  // Wire door buttons -------------------------------------------------
  document.querySelectorAll('.door-list .btn').forEach(btn => {
    btn.addEventListener('click', () => {
      fetch('/api/door', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({door_key: btn.dataset.door, action: btn.dataset.action})
      });
    });
  });

  // Initial fetch + SSE ----------------------------------------------
  Promise.all([
    fetch('/api/vehicles').then(r => r.json()).then(applyVehicles),
    fetch('/api/doors').then(r => r.json()).then(applyDoors),
  ]).then(() => {
    const es = new EventSource('/stream');
    es.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.event === 'vehicles') applyVehicles(msg.data);
        else if (msg.event === 'doors') applyDoors(msg.data);
        else if (msg.event === 'inbox') {
          // The inbox page handles its own refresh; nothing to do here
          // except maybe flash a tiny toast.
        }
      } catch (e) { /* ignore parse errors */ }
    };
    es.onerror = () => { /* browser will auto-reconnect */ };
  });
})();
