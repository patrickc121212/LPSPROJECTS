/* Live map + SSE client for Dad's Tesla Tracker.
 *
 * - Subscribes to /stream and reacts to three event types:
 *     'vehicles' → {vehicle_key, latitude, longitude, speed_mph, battery_pct, online}
 *     'doors'    → [{door_key, is_open, updated_at}, ...]
 *     'inbox'    → {recipient_key}
 * - Vehicle data may arrive partially (some vehicles have no fix yet),
 *   so we merge into a client-side state map.
 * - The map follows whichever car is being driven. Panning or zooming by
 *   hand turns following off, because a map that keeps yanking itself back
 *   is worse than one that sits still.
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

  // Following ----------------------------------------------------------
  // `pinned` is a car the user picked from the list; it wins over the
  // driving heuristic until they pick it again to release it.
  let following = true;
  let pinned = null;
  let focused = null;
  const MOVING_MPH = 1;            // below this is GPS noise on a parked car
  const RECENT_S = 300;            // a car that just stopped is still "the" car

  function isDriving(row) {
    if (!row || row.latitude == null) return false;
    if ((row.speed_mph || 0) > MOVING_MPH) return true;
    // Gear is the honest signal: the cars only report Location every 10 m,
    // so a car crawling in traffic can read 0 mph between fixes.
    return row.gear === 'D' || row.gear === 'R';
  }

  function drivenVehicle() {
    if (pinned) return pinned;
    const live = Object.values(state).filter(isDriving);
    if (live.length) {
      // Fastest wins. With two cars out, the one actually on a road is the
      // more interesting of the two.
      live.sort((a, b) => (b.speed_mph || 0) - (a.speed_mph || 0));
      return live[0].vehicle_key;
    }
    // Nobody is driving. Keep showing whoever moved last, briefly, so the
    // view does not jump away the moment someone parks.
    const now = Date.now() / 1000;
    const recent = Object.values(state)
      .filter(r => r.position_at && (now - r.position_at) < RECENT_S)
      .sort((a, b) => b.position_at - a.position_at);
    return recent.length ? recent[0].vehicle_key : focused;
  }

  function render() {
    const want = drivenVehicle();
    if (want && want !== focused) focused = want;

    document.querySelectorAll('#vehicle-list li').forEach(li => {
      const driving = isDriving(state[li.dataset.key]);
      li.classList.toggle('is-focused', li.dataset.key === focused);
      li.classList.toggle('is-driving', driving);
      li.classList.toggle('is-pinned', li.dataset.key === pinned);
    });

    const banner = document.getElementById('follow-banner');
    if (banner) {
      const v = vehicleByKey[focused];
      banner.hidden = following;
      banner.textContent = v ? `Follow ${v.label}` : 'Resume following';
    }

    if (!following || !focused) return;
    const row = state[focused];
    if (row && row.latitude != null && row.longitude != null) {
      // panTo keeps the user's zoom; setView would fight their choice.
      map.panTo([row.latitude, row.longitude], {animate: true});
    }
  }

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

      // Is anyone in it. The verdict is computed server-side (occupancy.py)
      // so this list, the SSE stream and /api/vehicles cannot disagree.
      const pres = document.getElementById('presence-' + k);
      if (pres && row.presence) {
        pres.textContent = row.presence.label;
        pres.title = row.presence.detail || '';
        pres.className = 'presence is-' + (row.presence.state || 'unknown');
      }

      if (row.latitude != null && row.longitude != null) {
        markers[k].setLatLng([row.latitude, row.longitude]);
      }
    });
    render();
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

  // Hand control back to the user the moment they touch the map ------
  // dragstart and zoomstart, deliberately, rather than movestart: panTo
  // above fires move events itself, so following would switch itself off on
  // the first position update. These two only fire for a real gesture.
  ['dragstart', 'zoomstart'].forEach(ev => {
    map.on(ev, () => {
      following = false;
      render();
    });
  });

  // Click a car to pin the view to it; click it again to release.
  document.querySelectorAll('#vehicle-list li').forEach(li => {
    li.addEventListener('click', () => {
      const k = li.dataset.key;
      pinned = (pinned === k) ? null : k;
      following = true;
      render();
    });
  });

  const banner = document.getElementById('follow-banner');
  if (banner) {
    banner.addEventListener('click', () => { following = true; render(); });
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
