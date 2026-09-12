// flax_post/web/static/app.js — petite-vue app for the post rack console.
// Renders the prototype layout (docs/post-ui-prototype.html) from the real
// /api/v1/blades feed. Discover = violet; grey = empty/unknown.
import { fetchBlades, fetchProfiles, saveSettings, postPower, postIdentify, fetchInventory,
         fetchArtifacts, fetchArtifact, fetchStep } from '/web-static/api.js';

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
    settingsAt: boot.settings_at || null,
    activeSwitch: '', sel: null, modal: null, filter: null, q: '',
    pwrChoice: null, pwrConfirm: false, idntMode: 'on', popProfile: '', solHeld: false,
    solHolder: null, solClientId: null, solLog: [], solIdle: null,
    // inventory (INV/POP): fetched on-demand for the SELECTED blade, not
    // precomputed per tile. invPort/invProfile track what `inv` was fetched
    // for, so loadInv() can no-op when neither the port nor the profile
    // changed (macinv is expensive -- never refetch from the 15s poll).
    inv: null, invPort: null, invProfile: '', invLoading: false, actionMsg: null,
    artifacts: null, artLoading: false,
    // the step modal's evidence block (/api/v1/step): status, headline, rows, notes
    stepInfo: null, stepLoading: false,

    // ---- data ----
    async mounted() {
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.modal) this.closeModal(); });
      this.profiles = await fetchProfiles(); await this.refresh(); setInterval(() => this.refresh(), REFRESH_MS);
    },
    // 15s poll of /api/v1/blades. Deliberately does NOT touch `inv` — macinv
    // is expensive, so the cached inventory for `sel.port` just stays put;
    // loadInv() is the only path that (re)fetches it.
    async refresh() {
      try {
        const d = await fetchBlades();
        this.slots = d.slots || []; this.racks = d.racks || {};
        if (this.sel) this.sel = this.slots.find((s) => s.port === this.sel.port) || null;
        if (d.settings) this.applySettings(d.settings);
      } catch (e) { console.error(e); }
    },
    // The server's operator context wins over this browser's copy, except for
    // the one field the operator is typing in right now (a poll must not eat
    // a half-typed order number). Set in browser A, visible in browser B
    // within one poll.
    applySettings(s) {
      const active = (document.activeElement && document.activeElement.name) || '';
      if (active !== 'post-order') this.order_no = s.order_no || '';
      if (active !== 'post-population') this.population = s.population || '';
      if (active !== 'post-customer') this.customer = s.customer || '';
      this.settingsAt = s.updated_at || null;
    },
    settingsAtText() {
      if (!this.settingsAt) return '';
      const d = new Date(this.settingsAt); return isNaN(d) ? '' : 'set ' + d.toLocaleString();
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
    // One phase's colour comes ONLY from that phase's own steps (ruling
    // 2026-09-12): red on any fault, green only when every step is done or
    // skipped, amber while it is the phase in flight, grey otherwise. A phase
    // to the left of the current one is NOT green by position — a latched
    // pass with holes in Discover reads grey there, never green.
    phaseState(b, phaseName) {
      const v = Object.values((b && b.steps && b.steps[phaseName]) || {});
      if (v.includes('fault')) return 'fault';
      if (v.length > 0 && v.every((x) => x === 'done' || x === 'skip')) return 'done';
      return (b && this.phases.indexOf(phaseName) === this.pidx(b)) ? 'cur' : '';
    },
    phaseSegs(b) { return this.phases.map((p) => this.phaseState(b, p)); },
    stepSegs(b) {
      const s = (b.steps && b.steps[b.phase]) || {};
      // a skipped step fills its segment like a done one: it completes the phase
      return Object.values(s).map((st) => (st === 'done' || st === 'skip') ? 'done' : st === 'cur' ? 'cur' : st === 'fault' ? 'fault' : '');
    },
    stepEntries(b, phaseName) { const s = (b && b.steps && b.steps[phaseName]) || {}; return Object.keys(s).map((k) => ({ name: k, state: s[k] })); },
    phaseDot(b, phaseName) {
      const st = this.phaseState(b, phaseName);
      return st === 'done' ? 'done' : st === 'fault' ? 'fault' : st === 'cur' ? this.phaseKey(b) : 'grey';
    },
    phasePct(b, phaseName) {
      const s = (b && b.steps && b.steps[phaseName]) || {}; const v = Object.values(s);
      if (!v.length) return ''; const done = v.filter((x) => x === 'done' || x === 'skip').length;
      return done === v.length ? '✓' : `${done}/${v.length}`;
    },
    stepIcon(st) { return { done: '✓', cur: '◉', fault: '✕', pending: '·', skip: '–', unknown: '?' }[st] || '·'; },
    // the agent's skip reason for a Qualify step ('no storage' for fio on a
    // diskless blade), from the blade record's step_notes
    stepNote(b, name) { return (b && b.step_notes && b.step_notes[name]) || ''; },
    // the ladder's timing fault (or skipped-markers note) for a step: modal only
    faultNote(b, name) { return (b && b.fault_notes && b.fault_notes[name]) || ''; },
    _ladderSteps: ['power-on', 'tftp-seen', 'ipxe-seen', 'live-iso-seen', 'host-leased', 'host-pinged', 'host-ssh', 'bmc-ready', 'agent-reachable', 'bmc-updated', 'bios-updated', 'mlx-updated'],
    // the slot ladder as the step modal shows it: rung + clock + budget, each
    // boot mark with its offset from power-on, the skipped note, the fault
    // The ladder timeline as table rows (ruling 2026-09-12: it is everywhere,
    // so it must read at a glance): time of event | since power-on | event |
    // extra. Chronological; the rung the ladder is on NOW is the last row.
    ladderRows(b, name) {
      const v = b && b.ladder_view; if (!v || !v.rung || !this._ladderSteps.includes(name)) return [];
      const now = Date.now() / 1000;
      const t = (x) => x ? new Date(x * 1000).toLocaleTimeString() : '';
      const dur = (s) => { s = Math.max(0, Math.round(s)); return s >= 60 ? Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's' : s + 's'; };
      const since = (x) => (x && v.power_on_at) ? '+' + dur(x - v.power_on_at) : '';
      const rows = [];
      if (v.power_on_at) rows.push({ t: t(v.power_on_at), since: '+0s', event: 'power-on', extra: (b.ladder && b.ladder.human_power_on) ? 'by operator' : 'by the engine' });
      const names = { tftp: 'TFTP request seen', ipxe: 'iPXE script fetched', iso: 'live ISO fetched', ping: 'host answered ping', ssh: 'host answered ssh', bmcready: 'BMC answered a data read' };
      const marks = Object.entries(v.marks || {}).filter(([k, x]) => names[k] && x).sort((a, c) => a[1] - c[1]);
      for (const [k, x] of marks) rows.push({ t: t(x), since: since(x), event: names[k], extra: '' });
      if (v.skipped) rows.push({ t: '', since: '', event: 'boot markers not observed', extra: v.skipped });
      if (v.fault) rows.push({ t: t(v.fault.at), since: since(v.fault.at), event: '\u2715 ' + v.fault.rung, extra: v.fault.reason, cls: 'fault' });
      rows.push({ t: t(v.since), since: since(v.since), event: 'rung now: ' + v.rung,
                  extra: (v.rung === 'done' ? 'done since ' : 'for ') + dur(now - (v.since || now)) + (v.budget_s ? ', budget ' + v.budget_s + 's' : ''), cls: 'now' });
      return rows;
    },

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
    // INV button: red when a blocked part is installed (blocklist), like a failed POP
    invBtnCls() { return (this.inv && this.inv.blocked && this.inv.blocked.length) ? 'bad' : ''; },
    // a memory row matching a blocked DIMM (by slot when the run's dimmsum
    // judged it, else by serial+part) renders in error red
    dimmBlocked(m) {
      const bl = (this.inv && this.inv.blocked) || [];
      return bl.some((b) => (b.slot && m.slot && b.slot === m.slot) || (b.serial && b.part && b.serial === m.serial && b.part === m.part));
    },

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
      this._stopSolIdle();
      this.modal = { kind };
      this.actionMsg = null;
      if (kind === 'idnt') this.idntMode = 'on';
      if (kind === 'pop') { this.popProfile = ''; this.loadInv(); }
      if (kind === 'inv') this.loadInv();
      if (kind === 'pwr') { this.pwrChoice = null; this.pwrConfirm = false; }
      if (kind === 'sol') { this.solLog = []; this._openSol(this.sel.bmc_ip); }
      if (kind === 'sdr') this.loadArtifacts(['sdr-pre', 'sdr-post']);
    },
    // Qualify steps and the SDR modal read the captured evidence for the
    // blade's current run: list the artifacts for the stage(s), then fetch
    // each body. Digests first (they are the short, human-readable ones).
    async loadArtifacts(stages) {
      this.artifacts = null; this.artLoading = true;
      const port = this.sel && this.sel.port;
      if (!port) { this.artLoading = false; return; }
      try {
        const out = [];
        for (const stage of stages) {
          const list = await fetchArtifacts(port, stage);
          list.sort((a, b) => (a.kind === 'digest' ? 0 : 1) - (b.kind === 'digest' ? 0 : 1));
          for (const a of list) {
            const content = await fetchArtifact(port, stage, a.name);
            out.push({ ...a, content: content == null ? '' : content });
          }
        }
        this.artifacts = out;
      } catch (e) { console.error(e); this.artifacts = []; }
      finally { this.artLoading = false; }
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
        // "last console byte N s ago": a stale BMC-side SOL looks exactly like
        // an idle console; this makes the difference visible
        this._solIdleTimer = setInterval(() => { this.solIdle = window.SolConsole ? window.SolConsole.idleSeconds() : null; }, 1000);
      });
    },
    _stopSolIdle() { if (this._solIdleTimer) { clearInterval(this._solIdleTimer); this._solIdleTimer = null; } this.solIdle = null; },
    closeModal() {
      // Closing must never depend on anything else succeeding: the modal
      // goes away first, the SOL teardown is best-effort (an operator could
      // only reload the page when a step modal would not close, 2026-09-12).
      this.modal = null; this.artifacts = null; this.stepInfo = null;
      try { if (window.SolConsole) window.SolConsole.close(); } catch (e) { console.error(e); }
      try { this._stopSolIdle(); } catch (e) { console.error(e); }
      this.solLog = []; this.solHolder = null; this.solClientId = null;
    },
    // SDR dump -> rows whose status column is neither ok nor ns (the
    // lower/upper (non-)critical threshold crossings an operator must see),
    // parsed from `name | id | status | reading` lines.
    sdrAttention(content) {
      const out = [];
      for (const line of (content || '').split('\n')) {
        const f = line.split('|').map((x) => x.trim());
        if (f.length < 4) continue;
        const st = f[2].toLowerCase();
        if (!st || st === 'ok' || st === 'ns') continue;
        out.push({ name: f[0], st, cls: /cr/.test(st) ? 'cr' : 'nc', reading: f.slice(3).join(' | ') });
      }
      return out;
    },
    isSdr(a) { return a && (a.stage === 'sdr-pre' || a.stage === 'sdr-post'); },
    toggleArt(a) { a.open = !a.open; },
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
    openStep(phaseName, stepName) {
      this.modal = { kind: 'step', phase: phaseName, step: stepName };
      this.artifacts = null; this.stepInfo = null;
      this.loadStep(phaseName, stepName);
      if (phaseName === 'Qualify') this.loadArtifacts([stepName]);
    },
    async loadStep(phaseName, stepName) {
      const port = this.sel && this.sel.port; if (!port) return;
      this.stepLoading = true;
      try { this.stepInfo = await fetchStep(port, phaseName, stepName); }
      catch (e) { console.error(e); this.stepInfo = null; }
      finally { this.stepLoading = false; }
    },
    // raw artifacts over this size start collapsed (lspci -vv is 240 KB)
    artBig(a) { return (a.kind !== 'digest') && ((a.content || '').length > 4000); },
    stepWord(st) { return { done: 'passed', cur: 'in progress', pending: 'not reached', fault: 'failed', skip: 'skipped', unknown: 'no evidence' }[st] || st; },
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
