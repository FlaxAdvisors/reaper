// flax_rack/web/rackpost.js: petite-vue app for one rack order (layout = mockup rack-v3).
// Polls api/state (relative: the page lives at /<prefix>/) every 5 s. Only the
// populated OUs are drawn; a dashed marker stands for each run of skipped OUs.
// Plan 1 has rungs only in Discover; the other phases render empty/grey.

const POLL_MS = 5000;
const COLS = ['L', 'C', 'R'];
const COLNAME = { L: 'left', C: 'center', R: 'right' };
const RGB = { discover: '163,113,247', firmware: '88,166,255', qualify: '210,153,34',
              deploy: '57,197,207', done: '63,185,80', fault: '248,81,73', grey: '110,118,129' };
const KIND = { 'bc-head-tp': 'BC head' };
const SWKIND = { mgmt: 'mgmt 1G', tor: 'TOR' };

const pad = (n) => String(n).padStart(2, '0');
const span = (top, bot) => (top === bot ? pad(top) : `${pad(top)}–${pad(bot)}`);

async function api(path, body) {
  const r = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let j = {}; try { j = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function stepState(run) {
  if (!run) return 'pending';
  if (run.status === 'running') return 'cur';
  if (run.status === 'interrupted') return 'unknown';
  return { pass: 'done', skip: 'skip', fail: 'fault' }[run.verdict] || 'unknown';
}

function stepNote(run) {
  if (!run) return '';
  if (run.status === 'interrupted') return 'interrupted';
  const d = run.detail || {};
  if (run.verdict === 'fail') return d.error || [d.status, d.note].filter(Boolean).join(' ') || 'fail';
  if (d.power) return 'power ' + d.power;
  if (d.count != null) return d.count + ' MACs';
  return '';
}

// rows keyed by bottom OU; groups = contiguous OU runs of one family (switch / device)
function buildLayout(s) {
  const rows = [];
  for (const sw of s.switches || []) rows.push({ key: 'sw-' + sw.id, family: 'sw', layout: 'full', ou: sw.ou, top: sw.ou, sw });
  const lcr = {};
  for (const d of s.devices || []) {
    const top = d.ou + d.h - 1;
    if (d.col) {
      const r = (lcr[d.ou] ??= { key: 'lcr-' + d.ou, family: 'dev', layout: 'lcr', ou: d.ou, top, byCol: {} });
      r.top = Math.max(r.top, top); r.byCol[d.col] = d;
    } else {
      const ioms = Object.keys(d.links).filter((k) => /^iom\d+$/.test(k)).sort();
      const cells = ioms.length
        ? ioms.map((k, i) => ({ key: d.pos + '-' + k, dev: d, iom: k, idx: i, label: `${d.pos} ${k.toUpperCase()} · ${d.links[k]}` }))
        : [{ key: d.pos, dev: d, label: `${d.pos}${d.links.mezz0 ? ' · ' + d.links.mezz0 : ''}` }];
      rows.push({ key: 'dev-' + d.pos, family: 'dev', layout: ioms.length > 1 ? 'two' : 'full', ou: d.ou, top, cells });
    }
  }
  for (const r of Object.values(lcr)) {
    r.cells = COLS.map((c) => {
      const d = r.byCol[c];
      if (!d) return { key: c + pad(r.ou), empty: true, label: c + pad(r.ou) };
      return { key: d.pos, dev: d, label: `${d.pos}${d.links.mezz0 ? ' · ' + d.links.mezz0 : ''}`, kindLabel: KIND[d.kind] };
    });
    rows.push(r);
  }
  rows.sort((a, b) => b.top - a.top || b.ou - a.ou);
  const groups = [];
  let g = null;
  for (const r of rows) {
    r.label = pad(r.ou);
    if (g && r.family === g.family && r.top >= g.bottom - 1) {
      g.rows.push(r); g.bottom = Math.min(g.bottom, r.ou);
      continue;
    }
    const gap = g && r.top < g.bottom - 1 ? `┄ ${span(g.bottom - 1, r.top + 1)} ┄` : '';
    g = { family: r.family, top: r.top, bottom: r.ou, rows: [r], gap,
          heads: r.family === 'sw' ? ['switches'] : ['left', 'center', 'right'] };
    groups.push(g);
  }
  for (const x of groups) x.range = span(x.top, x.bottom);
  return groups;
}

function selFromHash() {
  const m = /(?:^#|&)sel=([A-Za-z0-9-]+)/.exec(location.hash);
  return m ? m[1] : null;
}

function App() {
  return {
    s: { switches: [], devices: [], phases: ['discover', 'firmware', 'qualify', 'deploy', 'done'], profiles: [], rack_runs: {}, events: [] },
    live: null, sel: selFromHash(), filter: null, q: '',
    actionMsg: null, hdrMsg: null, closing: false, confirmText: '', closeMsg: null, busy: false,
    metricList: [
      { key: 'discover', label: 'discover', title: 'devices in Discover' },
      { key: 'firmware', label: 'firmware', title: 'Plan 2' },
      { key: 'qualify', label: 'qualify', title: 'Plan 3' },
      { key: 'deploy', label: 'deploy', title: 'Plan 4' },
      { key: 'done', label: 'done', title: 'Plan 4' },
      { key: 'alert', label: 'alerts', title: 'devices with a failed rung' },
    ],

    async mounted() {
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { this.closing ? (this.closing = false) : this.close(); } });
      await this.refresh(); setInterval(() => this.refresh(), POLL_MS);
    },
    async refresh() {
      try { this.s = await api('api/state'); this.live = true; document.title = `${this.s.prefix} rackpost`; }
      catch (e) { this.live = false; console.error(e); }
    },

    get layout() { return buildLayout(this.s); },
    get selDev() { return this.sel ? (this.s.devices.find((d) => d.pos === this.sel) || null) : null; },

    // ---- per-device pipeline (null-safe: petite-vue may evaluate during teardown) ----
    steps(d, phase) {
      if (!d || phase !== 'discover') return {};
      const runs = d.runs || {}, rr = this.s.rack_runs || {};
      const out = {};
      for (const rung of this.s.discover_rungs || []) out[rung] = rung === 'switch-fdb' ? rr[rung] : runs[rung];
      return out;
    },
    stepEntries(d, phase) {
      return Object.entries(this.steps(d, phase)).map(([name, run]) => ({
        name, run, state: stepState(run), note: stepNote(run),
        title: run ? `${run.status}${run.verdict ? ' · ' + run.verdict : ''} · ${run.finished_at || run.started_at}` : 'not run' }));
    },
    phaseState(d, p) {
      const v = this.stepEntries(d, p).map((x) => x.state);
      if (v.includes('fault')) return 'fault';
      if (v.length && v.every((x) => x === 'done' || x === 'skip')) return 'done';
      if (v.some((x) => x !== 'pending')) return 'cur';
      return '';
    },
    phaseSegs(d) { return !d ? [] : this.s.phases.map((p) => this.phaseState(d, p)); },
    stepSegs(d) {
      return this.stepEntries(d, 'discover').map((x) => (x.state === 'done' || x.state === 'skip') ? 'done'
        : x.state === 'fault' ? 'fault' : x.state === 'cur' ? 'cur' : '');
    },
    faulted(d) { return this.phaseState(d, 'discover') === 'fault'; },
    phaseKey(d) {
      if (!d) return 'grey';
      if (this.faulted(d)) return 'fault';
      return Object.keys(d.runs || {}).length ? 'discover' : 'grey';
    },
    dotCls(d) { return this.phaseKey(d); },
    phaseDot(d, p) { const st = this.phaseState(d, p); return st === 'done' ? 'done' : st === 'fault' ? 'fault' : st === 'cur' ? p : 'grey'; },
    phasePct(d, p) {
      const v = this.stepEntries(d, p); if (!v.length) return '';
      return `${v.filter((x) => x.state === 'done' || x.state === 'skip').length}/${v.length}`;
    },
    stepIcon(st) { return { done: '✓', cur: '◉', fault: '✕', pending: '·', skip: '–', unknown: '?' }[st] || '·'; },
    statusText(d) {
      if (!d) return '';
      const bad = this.stepEntries(d, 'discover').find((x) => x.state === 'fault');
      if (bad) return `discover · ${bad.name}`;
      const cur = this.stepEntries(d, 'discover').find((x) => x.state !== 'done' && x.state !== 'skip');
      return cur ? `discover · ${cur.name}` : 'discover ✓';
    },
    statusStyle(d) { const c = RGB[this.phaseKey(d)] || RGB.discover; return `background:rgba(${c},.15);color:rgb(${c})`; },
    faults(d) {
      if (!d) return [];
      return this.stepEntries(d, 'discover').filter((x) => x.state === 'fault' || x.state === 'unknown')
        .map((x) => ({ rung: x.name, text: x.note, bad: x.state === 'fault' }));
    },

    // ---- header ----
    get counts() {
      const ds = this.s.devices || [];
      return { discover: ds.length, firmware: 0, qualify: 0, deploy: 0, done: 0,
               alert: ds.filter((d) => this.faulted(d)).length };
    },
    toggle(f) { this.filter = this.filter === f ? null : f; },
    inFilter(d, f) { return f === 'alert' ? this.faulted(d) : f === 'discover'; },
    stageText() {
      if (this.s.closed_at) return 'closed ' + new Date(this.s.closed_at).toLocaleString();
      if (this.s.stage_running) return 'discover running…';
      const e = (this.s.events || [])[0];
      return e ? `${e.text} · ${new Date(e.ts).toLocaleTimeString()}` : 'no stage run yet';
    },
    eventsTitle() { return (this.s.events || []).slice(0, 12).map((e) => `${e.ts} ${e.kind}: ${e.text}`).join('\n'); },

    // ---- tiles ----
    swKind(sw) { return `${SWKIND[sw.role] || sw.role} · ${sw.model} · ${sw.nos}`; },
    swDot(sw) {
      if (!sw.mgmt_ip) return 'grey';
      const r = (this.s.rack_runs || {})['switch-fdb'];
      return !r ? 'grey' : r.status === 'running' ? 'discover' : r.verdict === 'pass' ? 'done' : 'fault';
    },
    swTitle(sw) { const r = (this.s.rack_runs || {})['switch-fdb']; return r ? `switch-fdb ${r.verdict || r.status} · ${stepNote(r)}` : 'switch-fdb not run'; },
    cellMac(c) { return c.iom ? (c.dev.iom_macs || [])[c.idx] : c.dev.bmc_mac; },
    popTitle(d) {
      if (!d) return '';
      return d.pop_overridden ? `override: BOM part ${d.part} defaults to ${d.pop_default}` : `default from BOM part ${d.part}`;
    },
    chips(c) {
      const d = c.dev, seen = d.link_seen || {};
      const names = c.iom ? [c.iom] : Object.keys(d.links).sort();
      const out = [];
      for (const n of names) {
        const st = (seen[n] || {}).state || '';
        const title = `${n}: BOM ${d.links[n]} · seen ${(seen[n] || {}).text || 'not checked (Plan 2)'}`;
        const m = /^pcie(\d+)$/.exec(n);
        if (m) out.push({ label: `P${m[1]}→${d.links[n].split(':')[0]}`, state: st, title });
        else if (st) out.push({ label: st === 'ok' ? 'mgmt ✓' : 'mgmt ✕', state: st, title });
      }
      return out;
    },
    matches(d) {
      const q = this.q.trim().toLowerCase(); if (!q) return false;
      return [d.pos, d.host_mac, d.bmc_mac, ...(d.iom_macs || []), ...Object.values(d.names || {}), ...Object.values(d.addrs || {})]
        .some((v) => v && v.toLowerCase().includes(q));
    },
    get matchCount() { return (this.s.devices || []).filter((d) => this.matches(d)).length; },
    wrapCls(c) {
      if (c.empty) return 'empty';
      const cl = [];
      if (this.sel === c.dev.pos) cl.push('active');
      if (this.q) cl.push(this.matches(c.dev) ? 'match' : 'faded');
      else if (this.filter && !this.inFilter(c.dev, this.filter)) cl.push('faded');
      return cl.join(' ');
    },
    tileStyle(c) { return (!c.empty && this.sel === c.dev.pos) ? `background:rgba(${RGB[this.phaseKey(c.dev)] || RGB.discover},.12)` : ''; },
    colName(d) { return d ? (COLNAME[d.col] || 'full') : ''; },

    // ---- drawer ----
    open(c) {
      if (!c || c.empty) return;
      this.actionMsg = null;
      this.sel = this.sel === c.dev.pos ? null : c.dev.pos;
      history.replaceState(null, '', this.sel ? '#sel=' + this.sel : location.pathname);
    },
    close() { this.sel = null; history.replaceState(null, '', location.pathname); },
    addrRows(d) {
      if (!d) return [];
      const reserved = new Set((((d.runs || {}).reserve || {}).detail?.reserved || []).map((x) => x.role));
      const rr = (d.runs || {}).reserve;
      const mac = { host: d.host_mac, bmc: d.bmc_mac };
      const order = Object.keys(d.addrs).sort((a, b) => (a === 'bmc' ? -1 : b === 'bmc' ? 1 : a.localeCompare(b)));
      return order.map((role) => {
        const ok = reserved.has(role), failed = rr && rr.verdict === 'fail' && !ok;
        return { role: role.toUpperCase(), ip: d.addrs[role], name: (d.names || {})[role],
                 mac: role.startsWith('iom') ? (d.iom_macs || [])[Number(role.slice(3)) - 1] : mac[role],
                 mark: ok ? '✓ reserved' : failed ? '✕ reserve' : '—', cls: ok ? 'ok' : failed ? 'bad' : 'no' };
      });
    },
    linkRows(d) {
      if (!d) return [];
      const seen = d.link_seen || {};
      return Object.keys(d.links).sort().map((n) => ({
        name: n, expected: d.links[n],
        seen: (seen[n] || {}).text || (d.links[n].startsWith('C:') ? 'not run' : '— Plan 2'),
        state: (seen[n] || {}).state || 'dimv' }));
    },
    async setPop(d, value) {
      try { await api('api/pop', { pos: d.pos, profile: value === d.pop_default ? null : value }); }
      catch (e) { this.actionMsg = 'POP: ' + e.message; }
      await this.refresh();
    },
    async discover() {
      try { await api('api/stage/discover', {}); this.hdrMsg = 'discover started'; }
      catch (e) { this.hdrMsg = 'discover: ' + e.message; }
      await this.refresh();
    },
    async retry() {
      try { await api('api/stage/discover', {}); this.actionMsg = 'discover started'; }
      catch (e) { this.actionMsg = 'discover: ' + e.message; }
      await this.refresh();
    },

    // ---- close order ----
    openClose() { this.closing = true; this.confirmText = ''; this.closeMsg = null; },
    async doClose() {
      this.busy = true; this.closeMsg = 'closing…';
      try {
        const r = await api('api/close', { confirm: this.confirmText });
        this.closeMsg = `closed; ${r.released} reservation(s) released`;
      } catch (e) { this.closeMsg = 'close failed, order stays open: ' + e.message; }
      this.busy = false;
      await this.refresh();
    },
  };
}

function mount() {
  if (window.PetiteVue) { window.App = App; window.PetiteVue.createApp({ App }).mount('#app'); }
  else setTimeout(mount, 20);
}
mount();
