// flax_post/web/static/api.js
export async function fetchBlades() {
  const r = await fetch('/api/v1/blades', { cache: 'no-store' });
  if (!r.ok) throw new Error('blades ' + r.status);
  return r.json();                       // {switch, racks, slots}
}
export async function fetchProfiles() {
  const r = await fetch('/api/v1/profiles', { cache: 'no-store' });
  return r.ok ? (await r.json()).profiles : [];
}
export async function saveSettings(patch) {
  const r = await fetch('/api/v1/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  let j = {}; try { j = await r.json(); } catch (e) { /* empty body */ }
  return { status: r.status, ...j };
}
// A tile's own order ({port, order_no, population}) or
// {port, clear: true} to follow the header again.
export async function postAssign(body) {
  const r = await fetch('/api/v1/assign', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: r.status, ...(await r.json()) };
}
export async function fetchFinishPreview(order) {
  const r = await fetch('/api/v1/orders/finish-preview?order=' + encodeURIComponent(order), { cache: 'no-store' });
  return r.ok ? r.json() : { members: [], pending: [], no_serial: [] };
}
export async function postFinish(body) {
  const r = await fetch('/api/v1/orders/finish', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: r.status, ...(await r.json()) };
}
// includeFinished: finished orders too (the header/drawer label refill index).
export async function fetchOrders(includeFinished) {
  const r = await fetch('/api/v1/orders' + (includeFinished ? '?include_finished=1' : ''), { cache: 'no-store' });
  return r.ok ? r.json() : { orders: [], header: null };
}
// One ORDER's labels {order_no, sales, customer, population} (every node on
// it): overwrite, blank clears. -> {ok, finished}
export async function postOrderLabels(body) {
  const r = await fetch('/api/v1/orders/labels', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let j = {}; try { j = await r.json(); } catch (e) { /* empty body */ }
  return { status: r.status, ...j };
}
export async function postPower(port, action) {
  const r = await fetch('/api/v1/power', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port, action }),
  });
  return { status: r.status, ...(await r.json()) };
}
export async function postIdentify(port, mode) {
  const r = await fetch('/api/v1/identify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port, mode }),
  });
  return { status: r.status, ...(await r.json()) };
}
export async function postDeployRetry(port, mode) {
  const r = await fetch('/api/v1/deploy/retry', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port, mode }),
  });
  return { status: r.status, ...(await r.json()) };
}
export async function fetchInventory(port, profile) {
  const q = profile ? ('?profile=' + encodeURIComponent(profile)) : '';
  const r = await fetch('/api/v1/inventory/' + encodeURIComponent(port) + q, { cache: 'no-store' });
  if (!r.ok) return { present: false, error: r.status };
  return r.json();
}
// Captured qualify evidence for the blade's CURRENT run (post_artifact via the
// viewer): the list for one stage, then one artifact's text body.
export async function fetchArtifacts(port, stage) {
  const r = await fetch('/api/v1/artifact?port=' + encodeURIComponent(port)
    + '&stage=' + encodeURIComponent(stage), { cache: 'no-store' });
  if (!r.ok) return [];
  return (await r.json()).artifacts || [];
}
export async function fetchArtifact(port, stage, name) {
  const r = await fetch('/api/v1/artifact?port=' + encodeURIComponent(port)
    + '&stage=' + encodeURIComponent(stage) + '&name=' + encodeURIComponent(name), { cache: 'no-store' });
  if (!r.ok) return null;
  return (await r.json()).content;
}

export async function fetchStep(port, phase, step) {
  const r = await fetch('/api/v1/step?port=' + encodeURIComponent(port) + '&phase=' + encodeURIComponent(phase)
    + '&step=' + encodeURIComponent(step), { cache: 'no-store' });
  if (!r.ok) return null;
  return r.json();
}
