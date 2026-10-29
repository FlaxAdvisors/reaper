// flax_rack/web/rackpost.js: petite-vue app for one rack order (layout = mockup rack-v3).
// Polls api/state (relative: the page lives at /<prefix>/) every 5 s. Only the
// populated OUs are drawn; a dashed marker stands for each run of skipped OUs.
// Phases with rungs: discover, firmware (fw-versions, report only), qualify (the
// device's qualify_rungs from the server). deploy/done render empty/grey (Plan 3/4).

const POLL_MS = 5000;
// geometry cols are ints 1..5 (L/C/R = 1/2/3); a group of rows is drawn with
// max(3, highest col used in it) columns so an L/C/R rack looks as before.
const COLNAME = { 1: 'left', 2: 'center', 3: 'right' };
const COLTAG = { 1: 'L', 2: 'C', 3: 'R' };
const colHead = (c, n) => (n <= 3 ? COLNAME[c] : `col ${c}`);
const emptyLabel = (c, ou) => (COLTAG[c] ? COLTAG[c] + pad(ou) : `col${c} · ${pad(ou)}`);
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

const gbit = (b) => (b == null ? '?' : (b / 1e9).toFixed(b < 1e10 ? 2 : 1));

function stepNote(run, name) {
  if (!run) return '';
  if (run.status === 'interrupted') return 'interrupted';
  const d = run.detail || {};
  if (run.verdict === 'fail') return d.error || [d.status, d.note].filter(Boolean).join(' ') || 'fail';
  if (run.verdict === 'skip') return d.reason || 'skipped';
  if (name === 'fw-versions') return `BIOS ${d.bios_version} · BMC ${d.bmc_firmware}`;
  if (name === 'pop') return d.profile || '';
  if (name === 'iperf-mesh') {
    if (d.served) return `served ${d.served.length}`;
    return ['mgmt', 'A', 'B'].filter((n) => d[n]).map((n) => `${n} ${gbit(d[n].tx_bps)}/${gbit(d[n].rx_bps)}G`).join(' · ');
  }
  if (name === 'fabric-addr') return Object.values(d).filter((x) => typeof x === 'string').join(' ');
  if (name === 'pxe-live' && d.ssh_after_s != null) return `ssh after ${d.ssh_after_s}s`;
  if (d.power) return 'power ' + d.power;
  if (d.count != null) return d.count + (name && name.startsWith('lldp') ? ' neighbours' : ' MACs');
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
  for (const r of Object.values(lcr)) rows.push(r);
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
    g = { family: r.family, top: r.top, bottom: r.ou, rows: [r], gap };
    groups.push(g);
  }
  for (const x of groups) {
    const n = Math.max(3, ...x.rows.filter((r) => r.byCol).flatMap((r) => Object.keys(r.byCol).map(Number)));
    const cols = Array.from({ length: n }, (_, i) => i + 1);
    x.heads = x.family === 'sw' ? ['switches'] : cols.map((c) => colHead(c, n));
    x.style = x.family !== 'sw' && n > 3 ? `grid-template-columns:46px repeat(${n},1fr)` : '';
    for (const r of x.rows) {
      if (!r.byCol) continue;
      r.style = x.style;
      r.cells = cols.map((c) => {
        const d = r.byCol[c];
        if (!d) return { key: 'e' + c + '-' + pad(r.ou), empty: true, label: emptyLabel(c, r.ou) };
        return { key: d.pos, dev: d, label: `${d.pos}${d.links.mezz0 ? ' · ' + d.links.mezz0 : ''}`, kindLabel: KIND[d.kind] };
      });
    }
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
    live: null, sel: selFromHash(), filter: null, q: '', popOpen: null, now: Date.now(),
    actionMsg: null, hdrMsg: null, closing: false, confirmText: '', closeMsg: null, busy: false,
    qualifying: false, qualText: '',
    metricList: [
      { key: 'discover', label: 'discover', title: 'devices in Discover' },
      { key: 'firmware', label: 'firmware', title: 'devices with firmware versions read (report only)' },
      { key: 'qualify', label: 'qualify', title: 'devices in Qualify' },
      { key: 'deploy', label: 'deploy', title: 'Plan 4' },
      { key: 'done', label: 'done', title: 'Plan 4' },
      { key: 'alert', label: 'alerts', title: 'devices with a failed rung' },
    ],

    async mounted() {
      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (this.qualifying) this.qualifying = false; else if (this.closing) this.closing = false; else if (this.popOpen) this.popOpen = null; else this.close();
      });
      await this.refresh(); setInterval(() => this.refresh(), POLL_MS);
    },
    async refresh() {
      try { this.s = await api('api/state'); this.live = true; this.now = Date.now(); document.title = `${this.s.prefix} rackpost`; }
      catch (e) { this.live = false; console.error(e); }
    },

    get layout() { return buildLayout(this.s); },
    get selDev() { return this.sel ? (this.s.devices.find((d) => d.pos === this.sel) || null) : null; },

    // ---- per-device pipeline (null-safe: petite-vue may evaluate during teardown) ----
    steps(d, phase) {
      if (!d) return {};
      const runs = d.runs || {}, rr = this.s.rack_runs || {};
      const out = {};
      if (phase === 'discover') {
        for (const rung of this.s.discover_rungs || []) out[rung] = rung === 'switch-fdb' ? rr[rung] : runs[rung];
      } else if (phase === 'firmware') {
        if (!(d.qualify_rungs || []).length) return {};
        for (const rung of this.s.firmware_rungs || []) out[rung] = runs[rung];
      } else if (phase === 'qualify') {
        for (const rung of d.qualify_rungs || []) out[rung] = runs[rung];
      }
      return out;
    },
    stepEntries(d, phase) {
      return Object.entries(this.steps(d, phase)).map(([name, run]) => ({
        name, run, state: stepState(run), note: stepNote(run, name),
        arts: Object.entries((run && run.artifacts) || {}).map(([n, url]) => ({ name: n, url })),
        title: run ? `${run.status}${run.verdict ? ' · ' + run.verdict : ''} · ${run.finished_at || run.started_at}` : 'not run' }));
    },
    hasRuns(d, phase) { return this.stepEntries(d, phase).some((x) => x.run); },
    // the phase whose steps the tile's second bar shows: the latest one with a run
    curPhase(d) { return this.hasRuns(d, 'qualify') ? 'qualify' : 'discover'; },
    phaseState(d, p) {
      const v = this.stepEntries(d, p).map((x) => x.state);
      if (v.includes('fault')) return 'fault';
      if (v.length && v.every((x) => x === 'done' || x === 'skip')) return 'done';
      if (v.some((x) => x !== 'pending')) return 'cur';
      return '';
    },
    phaseSegs(d) { return !d ? [] : this.s.phases.map((p) => this.phaseState(d, p)); },
    stepSegs(d) {
      if (!d) return [];
      return this.stepEntries(d, this.curPhase(d)).map((x) => (x.state === 'done' || x.state === 'skip') ? 'done'
        : x.state === 'fault' ? 'fault' : x.state === 'cur' ? 'cur' : '');
    },
    faulted(d) { return !!d && ['discover', 'firmware', 'qualify'].some((p) => this.phaseState(d, p) === 'fault'); },
    phaseKey(d) {
      if (!d) return 'grey';
      if (this.faulted(d)) return 'fault';
      if (this.hasRuns(d, 'qualify')) return this.phaseState(d, 'qualify') === 'done' ? 'done' : 'qualify';
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
      for (const p of ['discover', 'firmware', 'qualify']) {
        const bad = this.stepEntries(d, p).find((x) => x.state === 'fault');
        if (bad) return `${p} · ${bad.name}`;
      }
      const p = this.curPhase(d);
      const cur = this.stepEntries(d, p).find((x) => x.state !== 'done' && x.state !== 'skip');
      return cur ? `${p} · ${cur.name}` : `${p} ✓`;
    },
    statusStyle(d) { const c = RGB[this.phaseKey(d)] || RGB.discover; return `background:rgba(${c},.15);color:rgb(${c})`; },
    faults(d) {
      if (!d) return [];
      return ['discover', 'firmware', 'qualify'].flatMap((p) => this.stepEntries(d, p))
        .filter((x) => x.state === 'fault' || x.state === 'unknown')
        .map((x) => ({ rung: x.name, text: x.note, bad: x.state === 'fault' }));
    },
    popRun(d) { return d && (d.runs || {}).pop; },
    popMark(d) {
      const r = this.popRun(d);
      if (!r || r.status !== 'done') return { t: '·', cls: '', title: 'POP not run yet (Qualify)' };
      const x = r.detail || {};
      if (r.verdict === 'pass') return { t: '✓', cls: 'ok', title: `POP ${x.profile}: every rule found` };
      if (r.verdict === 'skip') return { t: '–', cls: '', title: 'POP skipped: ' + (x.reason || '') };
      return { t: '✕', cls: 'bad', title: 'POP: ' + (x.error || 'fail') };
    },
    openPop(d) { if (this.popRun(d) && this.popRun(d).detail) this.popOpen = d.pos; },
    get popDev() { return this.popOpen ? (this.s.devices.find((d) => d.pos === this.popOpen) || null) : null; },

    // ---- firmware versions (report only): equal versions share a colour, like INV ----
    fwOf(d) {
      const r = d && (d.runs || {})['fw-versions'];
      return r && r.verdict === 'pass' && r.detail ? r.detail : null;
    },
    fwKey(f) { return `${f.bios_version}|${f.bmc_firmware}`; },
    get fwGroups() {
      const keys = [...new Set((this.s.devices || []).map((d) => this.fwOf(d)).filter(Boolean).map((f) => this.fwKey(f)))].sort();
      return Object.fromEntries(keys.map((k, i) => [k, 'grp-' + (i % 9)]));
    },
    fwText(d) { const f = this.fwOf(d); return f ? `${f.bios_version} · ${f.bmc_firmware}` : ''; },
    fwCls(d) { const f = this.fwOf(d); return f ? this.fwGroups[this.fwKey(f)] : ''; },
    fwTitle(d) {
      const f = this.fwOf(d);
      return f ? `BIOS ${f.bios_version} (${f.bios_date}) · BMC ${f.bmc_firmware}${f.bmc_aux ? ' aux ' + f.bmc_aux : ''} · report only` : '';
    },

    // ---- BMC power chip from the poller (stale after 3 intervals -> grey ?) ----
    power(c) {
      const d = c.dev;
      if (!(d.addrs || {}).bmc) return null;
      const b = d.bmc_live || {}, iv = (this.s.poll_interval_s || 60) * 1000;
      if (!b.at) return { t: '?', cls: 'unk', title: 'BMC not polled yet' };
      const age = this.now - Date.parse(b.at);
      if (age > 3 * iv) return { t: '?', cls: 'unk', title: `stale: last BMC poll ${b.at}` };
      if (b.reachable === false) return { t: 'bmc ✕', cls: 'bad', title: `BMC ${d.addrs.bmc} no ping reply · ${b.at}` };
      if (!b.power) return { t: '?', cls: 'unk', title: `BMC answers ping, no power reading · ${b.at}` };
      return { t: b.power, cls: b.power === 'on' ? 'on' : 'off', title: `chassis power ${b.power} · ${b.at}` };
    },

    // ---- header ----
    get counts() {
      const ds = this.s.devices || [];
      return { discover: ds.length, firmware: ds.filter((d) => this.hasRuns(d, 'firmware')).length,
               qualify: ds.filter((d) => this.hasRuns(d, 'qualify')).length, deploy: 0, done: 0,
               alert: ds.filter((d) => this.faulted(d)).length };
    },
    toggle(f) { this.filter = this.filter === f ? null : f; },
    inFilter(d, f) {
      if (f === 'alert') return this.faulted(d);
      if (f === 'firmware' || f === 'qualify') return this.hasRuns(d, f);
      return f === 'discover';
    },
    qualifyTitle() {
      const q = this.s.qualify || {};
      const base = 'run Qualify: live boot → battery → POP per node, then wedge LLDP → HA path → fabric addresses → iperf mesh';
      return q.passed ? base + ' · rack passed' : (q.not_passed || []).length ? `${base} · not passed: ${q.not_passed.join(', ')}` : base;
    },
    stageText() {
      if (this.s.closed_at) return 'closed ' + new Date(this.s.closed_at).toLocaleString();
      if (this.s.stage_running) return `${this.s.stage || 'stage'} running…`;
      const e = (this.s.events || [])[0];
      return e ? `${e.text} · ${new Date(e.ts).toLocaleTimeString()}` : 'no stage run yet';
    },
    eventsTitle() { return (this.s.events || []).slice(0, 12).map((e) => `${e.ts} ${e.kind}: ${e.text}`).join('\n'); },

    // ---- tiles ----
    swKind(sw) { return `${SWKIND[sw.role] || sw.role} · ${sw.model} · ${sw.nos}`; },
    swRun(sw) { return (this.s.rack_runs || {})[sw.role === 'tor' ? 'lldp-' + sw.id : 'switch-fdb']; },
    swDot(sw) {
      const r = this.swRun(sw);
      if (!sw.mgmt_ip && !r) return 'grey';
      return !r ? 'grey' : r.status === 'running' ? (sw.role === 'tor' ? 'qualify' : 'discover') : r.verdict === 'pass' ? 'done' : 'fault';
    },
    swTitle(sw) {
      const n = sw.role === 'tor' ? 'lldp-' + sw.id : 'switch-fdb', r = this.swRun(sw);
      return r ? `${n} ${r.verdict || r.status} · ${stepNote(r, n)}` : `${n} not run`;
    },
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
        const title = `${n}: BOM ${d.links[n]} · seen ${(seen[n] || {}).text || 'not checked yet'}`;
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
    colName(d) { return !d ? '' : d.col ? (COLNAME[d.col] || `col ${d.col}`) : 'full'; },

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
        seen: (seen[n] || {}).text || 'not run',
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
    // Qualify power-cycles nodes, clears SELs and runs stress: the rack-wide run takes
    // the typed order id (like Close); a drawer (per-node) run a plain confirm naming it.
    qualHosts() { return (this.s.devices || []).filter((d) => (d.qualify_rungs || []).length); },
    openQualify() { this.qualifying = true; this.qualText = ''; },
    async doQualify() {
      this.qualifying = false;
      try { await api('api/stage/qualify', { confirm: this.qualText }); this.hdrMsg = 'qualify started'; }
      catch (e) { this.hdrMsg = 'qualify: ' + e.message; }
      await this.refresh();
    },
    async qualify(pos) {
      if (!window.confirm(`Re-qualify ${pos}? It is power-cycled into the live image, its SEL is ` +
                          'cleared and it runs the stress battery.')) return;
      try { await api('api/stage/qualify', { positions: [pos], confirm: true }); this.actionMsg = `qualify started for ${pos}`; }
      catch (e) { this.actionMsg = 'qualify: ' + e.message; }
      await this.refresh();
    },
    openInv(d) { const u = this.invUrl(d); if (u) window.open(u, '_blank', 'noopener'); },
    invUrl(d) {
      const a = ((d && (d.runs || {}).inventory) || {}).artifacts || {};
      return a['macinv-v'] || a.macinv || null;
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
