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
  await fetch('/api/v1/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
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
export async function fetchInventory(port, profile) {
  const q = profile ? ('?profile=' + encodeURIComponent(profile)) : '';
  const r = await fetch('/api/v1/inventory/' + encodeURIComponent(port) + q, { cache: 'no-store' });
  if (!r.ok) return { present: false, error: r.status };
  return r.json();
}
