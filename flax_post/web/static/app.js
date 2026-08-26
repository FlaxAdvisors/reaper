// flax_post/web/static/app.js — petite-vue app for the post rack console.
// Renders the prototype layout (docs/post-ui-prototype.html) from the real
// /api/v1/blades feed. Discover = violet; grey = empty/unknown.
import { fetchBlades, fetchProfiles, saveSettings, postPower, postIdentify, fetchInventory } from '/web-static/api.js';

// firmware phases (post_state fw_bmc/fw_bios/fw_nic 'phase') during which a
// power-off must be blocked -- mirrors flax_post/actions.py FW_ACTIVE.
const FW_ACTIVE_PHASES = ['checking', 'flashing', 'monitoring', 'activating'];

const COL = { L: 0, C: 1, R: 2, A: 0, B: 1, D: 3, full: 0 };
const COLNAME = { L: 'Left', C: 'Center', R: 'Right', A: 'A', B: 'B', D: 'D', full: '' };
const RGB = { discover: '163,113,247', firmware: '88,166,255', qualify: '210,153,34', done: '63,185,80', fault: '248,81,73' };
const REFRESH_MS = 15000;

const real = (slots) => slots.filter((s) => !s.empty);

// groupColor() memo: distinct-value -> color-index map, computed once per
// (section-array, keyField). Keyed by the section array's identity (each
// inventory fetch produces fresh arrays; petite-vue hands out a stable
// reactive proxy per underlying array, so the key is stable within one inv
// and old entries drop out via the WeakMap when a new inv replaces them).
const GRP_CACHE = new WeakMap();
function groupIndex(section, keyField, value) {
  let byField = GRP_CACHE.get(section);
  if (!byField) { byField = {}; GRP_CACHE.set(section, byField); }
  let map = byField[keyField];
  if (!map) {
    map = new Map(); let i = 0;
    for (const row of section) { const v = row[keyField]; if (!map.has(v)) map.set(v, i++); }
    byField[keyField] = map;
  }
  const idx = map.get(value);
  return idx == null ? 0 : idx % 9;
}

function buildGroups(slots) {
  const byG = {};
  for (const s of slots) { (byG[s.group] ??= {}); ((byG[s.group])[s.ou] ??= []).push(s); }
  return Object.keys(byG).map(Number).sort((a, b) => b - a).map((g) => {
    const ous = Object.keys(byG[g]).map(Number).sort((a, b) => b - a);
    const rows = ous.map((ou) => ({ ou, cells: byG[g][ou].slice().sort((a, b) => COL[a.col] - COL[b.col]) }));
    const cols = byG[g][ous[0]].map((c) => COLNAME[c.col] || c.col);
    const lo = ous[ous.length - 1];
    return { gid: g, range: `${lo}–${ous[0] + 1}`, colHeads: cols, rows };
  });
}

function App() {
  const boot = window.BOOT || {};
  return {
    boot,
    site: boot.site || 'Post',
    phases: boot.phases || ['Discover', 'Firmware', 'Qualify', 'Done'],
    slots: [], racks: {}, profiles: [],
    order_no: boot.order_no || '',
    population: boot.population || '',
    customer: boot.customer || '',
    activeSwitch: '', sel: null, modal: null, filter: null, q: '',
    pwrChoice: null, pwrConfirm: false, idntMode: 'on', popProfile: '', solHeld: false,
    solHolder: null, solClientId: null, solLog: [],
    // inventory (INV/POP): fetched on-demand for the SELECTED blade, not
    // precomputed per tile. invPort/invProfile track what `inv` was fetched
    // for, so loadInv() can no-op when neither the port nor the profile
    // changed (macinv is expensive -- never refetch from the 15s poll).
    inv: null, invPort: null, invProfile: '', invLoading: false, actionMsg: null,

    // ---- data ----
    async mounted() { this.profiles = await fetchProfiles(); await this.refresh(); setInterval(() => this.refresh(), REFRESH_MS); },
    // 15s poll of /api/v1/blades. Deliberately does NOT touch `inv` — macinv
    // is expensive, so the cached inventory for `sel.port` just stays put;
    // loadInv() is the only path that (re)fetches it.
    async refresh() {
      try {
        const d = await fetchBlades();
        this.slots = d.slots || []; this.racks = d.racks || {};
        if (this.sel) this.sel = this.slots.find((s) => s.port === this.sel.port) || null;
      } catch (e) { console.error(e); }
    },

    get rackList() { return Object.entries(this.racks).map(([sw, r]) => ({ switch: sw, label: r.label })); },
    get visible() { return this.activeSwitch ? this.slots.filter((s) => s.switch === this.activeSwitch) : this.slots; },
    get groups() { return buildGroups(this.visible); },
    get counts() {
      const b = real(this.visible), n = (ph) => b.filter((x) => x.phase === ph).length;
      return { discover: n('Discover'), firmware: n('Firmware'), qualify: n('Qualify'), done: n('Done'),
               alert: b.filter((x) => this.hasSel(x) || (x.alerts && x.alerts.length)).length };
    },
    get matchCount() { return real(this.visible).filter((b) => this.matches(b)).length; },

    // ---- phase / step helpers (null-safe: petite-vue may evaluate during teardown) ----
    pidx(b) { return b ? Math.max(0, this.phases.indexOf(b.phase)) : 0; },
    faulted(b) { const s = b && b.steps && b.steps[b.phase]; return !!s && Object.values(s).includes('fault'); },
    phaseKey(b) { return !b ? 'grey' : this.faulted(b) ? 'fault' : (b.phase || 'discover').toLowerCase(); },
    phaseSegs(b) {
      const cur = this.pidx(b), fault = this.faulted(b);
      return this.phases.map((p, i) => (fault && i === cur) ? 'fault' : i < cur ? 'done' : i === cur ? 'cur' : '');
    },
    stepSegs(b) {
      const s = (b.steps && b.steps[b.phase]) || {};
      return Object.values(s).map((st) => st === 'done' ? 'done' : st === 'cur' ? 'cur' : st === 'fault' ? 'fault' : '');
    },
    stepEntries(b, phaseName) { const s = (b && b.steps && b.steps[phaseName]) || {}; return Object.keys(s).map((k) => ({ name: k, state: s[k] })); },
    phaseDot(b, phaseName) {
      const i = this.phases.indexOf(phaseName), cur = this.pidx(b);
      return i < cur ? 'done' : i === cur ? this.phaseKey(b) : 'grey';
    },
    phasePct(b, phaseName) {
      const s = (b && b.steps && b.steps[phaseName]) || {}; const v = Object.values(s);
      if (!v.length) return ''; const done = v.filter((x) => x === 'done').length;
      return done === v.length ? '✓' : `${done}/${v.length}`;
    },
    stepIcon(st) { return { done: '✓', cur: '◉', fault: '✕', pending: '·' }[st] || '·'; },

    // ---- tile presentation (null-safe) ----
    colName(b) { return b ? (COLNAME[b.col] || b.col) : ''; },
    wattCls(b) { return (b.power_on === 'on') ? 'on' : (b.power_on === 'off') ? 'off' : 'unk'; },
    tilePwr(b) { return b.watts || '—'; },
    fwText(b) {
      const s = b && b.fw && b.fw.bmc;
      if (!s || !s.phase) return 'not evaluated';
      const cur = s.current || '—', tgt = s.target || '—';
      return cur === tgt ? cur : (cur + ' → ' + tgt);
    },
    fwCls(b) {
      const s = b && b.fw && b.fw.bmc;
      return s && s.ver_class ? s.ver_class : 'ver-na';
    },
    dotCls(b) { return b.empty ? 'grey' : this.phaseKey(b); },
    wrapCls(b) {
      const cl = [];
      if (b.empty) return 'empty';
      if (this.sel && this.sel.port === b.port) cl.push('active');
      if (this.q) { this.matches(b) ? cl.push('match') : cl.push('faded'); }
      else if (this.filter) {
        const inF = this.filter === 'alert' ? (this.hasSel(b) || (b.alerts && b.alerts.length)) : this.phaseKey(b) === this.filter;
        if (!inF) cl.push('faded');
      }
      return cl.join(' ');
    },
    tileStyle(b) { return (!b.empty && this.sel && this.sel.port === b.port) ? `background:rgba(${RGB[this.phaseKey(b)] || RGB.discover},.16)` : ''; },

    // ---- filter + search ----
    toggle(f) { this.filter = this.filter === f ? null : f; },
    matches(b) {
      const q = this.q.trim().toLowerCase(); if (!q) return false;
      return [b.serial, b.bmc_mac, b.host_mac, ...(b.macs_seen || [])].some((v) => v && v.toLowerCase().includes(q));
    },

    // ---- detail panel ----
    open(b) {
      if (!b || b.empty) return;
      const closing = this.sel && this.sel.port === b.port;
      this.closeModal();
      // Reset the whole inventory/POP context on every open/close/switch so a
      // non-default profile picked in one blade's POP modal never carries into
      // the next blade's background fetch (would skew its POP button color /
      // verdict with no UI indicator). popProfile back to '' => fetch-on-open
      // uses the blade's own default (order-level) profile.
      this.inv = null; this.invPort = null; this.invProfile = ''; this.popProfile = ''; this.actionMsg = null;
      if (closing) { this.sel = null; return; }
      this.sel = b;
      this.loadInv();
    },
    statusText(s) { if (!s) return ''; return this.faulted(s) ? `fault · ${s.step || ''}` : `${s.phase}${s.step ? ' · ' + s.step : ''}`; },
    statusStyle(s) { const c = RGB[this.phaseKey(s)] || RGB.discover; return `background:rgba(${c},.15);color:rgb(${c})`; },
    hasSel(b) { return !!(b && b.sel && b.sel.length); },
    hasSdr(b) { return !!(b && b.sdr && Object.keys(b.sdr).length); },
    // one event per line, prefixed with its timestamp — readable for a 70-event SEL
    selLines(b) { return this.hasSel(b) ? b.sel.map((e) => [e.ts, e.event].filter(Boolean).join('  ')).join('\n') : ''; },
    // POP button color reads the verdict fetched into `this.inv` for the
    // SELECTED blade (the per-tile `b.pop` producer field was dropped; there
    // is only ever one selected blade, so no blade arg) -- grey until inv is
    // loaded / if the blade has no inventory capture yet.
    popBtnCls() { const st = this.inv && this.inv.present && this.inv.pop && this.inv.pop.state; return st === 'green' ? 'on' : st === 'red' ? 'bad' : 'unk'; },

    // ---- inventory (INV/POP) fetch lifecycle ----
    flashActive(b) {
      return !!(b && b.fw && ['bmc', 'bios', 'nic'].some((k) => b.fw[k] && FW_ACTIVE_PHASES.includes(b.fw[k].phase)));
    },
    async loadInv() {
      if (!this.sel) return;
      const port = this.sel.port, profile = this.popProfile || '';
      if (this.invPort === port && this.invProfile === profile) return;   // no-op: nothing changed
      this.invLoading = true;
      try {
        this.inv = await fetchInventory(port, profile);
        this.invPort = port; this.invProfile = profile;
      } catch (e) {
        console.error(e);
        this.inv = { present: false, error: 'fetch failed' };
      } finally {
        this.invLoading = false;
      }
    },
    // per-row color-index (grp-0..grp-8, cycling) by distinct value of
    // keyField within `section` -- identical components share a color,
    // mismatches stand out (triage's RenderParts idiom). The distinct-value
    // scan is memoized per (section, keyField) in GRP_CACHE, so this is an
    // O(1) map lookup per row rather than an O(n) rescan.
    groupColor(section, keyField, value) {
      return 'grp-' + (section ? groupIndex(section, keyField, value) : 0);
    },

    // ---- modals ----
    openModal(kind) {
      if (!this.sel) return;
      // any modal switch (incl. re-opening 'sol' itself) tears down a live
      // SOL socket first -- the action buttons stay reachable while a modal
      // is open, so PWR/INV/etc. can be clicked straight over an open SOL
      // console without going through closeModal()'s modal=null path.
      if (window.SolConsole) window.SolConsole.close();
      this.modal = { kind };
      this.actionMsg = null;
      if (kind === 'idnt') this.idntMode = 'on';
      if (kind === 'pop') { this.popProfile = ''; this.loadInv(); }
      if (kind === 'inv') this.loadInv();
      if (kind === 'pwr') { this.pwrChoice = null; this.pwrConfirm = false; }
      if (kind === 'sol') { this.solLog = []; this._openSol(this.sel.bmc_ip); }
    },
    // (Re)connect the SOL console to `ip`, reusing the modal's terminal ref.
    // Used by both openModal('sol') and solRelaunch()'s reconnect path.
    _openSol(ip) {
      this.solHeld = false; this.solHolder = null; this.solClientId = null;
      // $refs for the modal's v-if body aren't attached until petite-vue's
      // reactive DOM flush (a microtask) runs after this handler returns;
      // queue the SolConsole.open() one microtask behind it.
      queueMicrotask(() => {
        if (!window.SolConsole || !this.modal || this.modal.kind !== 'sol') return;
        window.SolConsole.open(this.$refs.solTerm, null, ip, {
          onLock: (holder, held) => { this.solHolder = holder; this.solHeld = held; },
          onEvent: (m) => { this.solLog.unshift(m); if (this.solLog.length > 200) this.solLog.pop(); },
          onClient: (sid) => { this.solClientId = sid; },
        });
      });
    },
    closeModal() {
      if (window.SolConsole) window.SolConsole.close();
      this.modal = null;
      this.solLog = []; this.solHolder = null; this.solClientId = null;
    },
    solLockState() { return !this.solHolder ? 'request' : this.solHeld ? 'release' : 'requestRelease'; },
    solLockLabel() {
      return { request: 'Request Lock', release: 'Release Lock', requestRelease: 'Request Lock Release' }[this.solLockState()];
    },
    solLock() {
      if (!window.SolConsole) return;
      const state = this.solLockState();
      if (state === 'request') window.SolConsole.lock();
      else if (state === 'release') window.SolConsole.unlock();
      else window.SolConsole.requestRelease();
    },
    solRelaunch() {
      if (!window.SolConsole) return;
      // The detail panel's sel.bmc_ip is kept current by the 15s poll. If it has
      // changed since the modal opened (device re-addressed / corrected ip / a
      // different MAC now at this slot), reconnect the client to the new ip —
      // Relaunch behaves like re-clicking SOL. If the ip is unchanged, ask the
      // server to respawn the session (recover a wedged SOL / reset the lock).
      const ip = this.sel && this.sel.bmc_ip;
      if (ip && ip !== window.SolConsole.currentIp()) {
        this.solLog.unshift(new Date().toLocaleTimeString() + '  reconnecting to ' + ip);
        this._openSol(ip);
      } else {
        window.SolConsole.relaunch();
      }
    },
    async doPower() {
      if (!this.sel || !this.pwrChoice) return;
      const { port } = this.sel, action = this.pwrChoice;
      try {
        const res = await postPower(port, action);
        if (res.ok) this.actionMsg = `power ${res.action} ok`;
        else if (res.blocked) this.actionMsg = res.reason || 'power-off blocked';
        else this.actionMsg = res.reason || res.output || `power ${action} failed`;
      } catch (e) {
        console.error(e);
        this.actionMsg = 'power request failed';
      }
      this.pwrConfirm = false;
      await this.refresh();
    },
    async doIdent() {
      if (!this.sel) return;
      try {
        const res = await postIdentify(this.sel.port, this.idntMode);
        this.actionMsg = res.ok ? `identify ${res.mode} ok` : (res.reason || res.output || 'identify failed');
      } catch (e) {
        console.error(e);
        this.actionMsg = 'identify request failed';
      }
    },
    openStep(phaseName, stepName) { this.modal = { kind: 'step', phase: phaseName, step: stepName }; },
    modalTitle() {
      const m = this.modal; if (!m) return ''; const id = this.sel ? (this.sel.serial || this.sel.port) : '';
      const names = { pwr: 'Power', sol: 'SOL console', inv: 'Inventory', pop: 'Population', idnt: 'Identify', sdr: 'SDR sensors', sel: 'SEL events', step: `${m.phase} · ${m.step}` };
      return `${names[m.kind] || m.kind} — ${id}`;
    },

    // ---- settings (order + population + customer) ----
    async saveOrder() { await saveSettings({ order_no: this.order_no || null }); this.refresh(); },
    async clearOrder() { this.order_no = ''; await saveSettings({ order_no: null }); this.refresh(); },
    async savePopulation() { await saveSettings({ population: this.population || null }); this.refresh(); },
    async saveCustomer() { await saveSettings({ customer: this.customer || null }); this.refresh(); },
  };
}

function mount() {
  if (window.PetiteVue) { window.App = App; window.PetiteVue.createApp({ App }).mount('#app'); }
  else setTimeout(mount, 20);
}
mount();
