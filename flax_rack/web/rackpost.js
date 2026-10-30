// flax_rack/web/rackpost.js: petite-vue app for one rack order (layout = mockup rack-v3).
// Polls api/state (relative: the page lives at /<prefix>/) every 5 s. Only the
// populated OUs are drawn; a dashed marker stands for each run of skipped OUs.
// Phases with rungs: discover, firmware (Post's 6 steps, detect only), qualify (the
// device's qualify_rungs from the server), deploy (its deploy_rungs: install, firstboot,
// install-console (the SOL scan; the raw SOL is its `sol` artifact), flax-user,
// ha-config, ha-test). A step whose target did not answer is `unmeasured` (verdict):
// grey "not measured", never a fault and never done. done renders empty/grey (Plan 4).
// Deploy's gate may be passed with a recorded waiver of named Qualify rungs: the tile
// and drawer show it as a "waived" chip (amber while the rung still has not passed).
// Every switch and every BMC endpoint opens its own drawer (sel in the URL hash):
// a device `R05`, one JBOD IOM `JBOD01.iom2`, a switch `_sw-A`, a wedge's BMC
// `_sw-A.bmc`. Each is an object carrying pos (= the sel key), runs and
// qualify_rungs, so one set of pipeline functions serves them all. A step row
// opens the evidence modal (Post's stepinfo: headline, rows, notes, artifacts);
// an artifact opens the in-page viewer (artM) over it, never a raw tab.

const POLL_MS = 5000;
// geometry cols are ints 1..5 (L/C/R = 1/2/3); a group of rows is drawn with
// max(3, highest col used in it) columns so an L/C/R rack looks as before.
const COLNAME = { 1: 'left', 2: 'center', 3: 'right' };
const COLTAG = { 1: 'L', 2: 'C', 3: 'R' };
const colHead = (c, n) => (n <= 3 ? COLNAME[c] : `col ${c}`);
const emptyLabel = (c, ou) => (COLTAG[c] ? COLTAG[c] + pad(ou) : `col${c} · ${pad(ou)}`);
const RGB = { discover: '163,113,247', firmware: '88,166,255', qualify: '210,153,34',
              deploy: '57,197,207', done: '63,185,80', fault: '248,81,73', grey: '110,118,129',
              unmeasured: '110,118,129' };
const KIND = { 'bc-head-tp': 'BC head' };
const SWKIND = { mgmt: 'mgmt 1G', tor: 'TOR' };
// a wedge BMC's own Discover rows (the rest of the switch ladder is the SONiC half)
const BMC_STEPS = ['switchportlink', 'bmc-mac-seen', 'bmc-reserved', 'bmc-leased', 'bmc-pinged', 'serial', 'bmc-ready'];
// the viewer reads at most this much of an artifact (the rest: Download)
const ART_MAX = 2 * 1024 * 1024;
const STEP_WORD = { done: 'done', cur: 'in progress', fault: 'fault', pending: 'not run', skip: 'skipped', unknown: 'unknown', unmeasured: 'not measured' };

const iomObj = (d, ep) => ({ type: 'iom', pos: `${d.pos}.${ep}`, dev: d, ep, n: Number(ep.slice(3)),
  runs: (d.endpoint_runs || {})[ep] || {}, qualify_rungs: [], view: (d.ioms || {})[ep] || {} });
const swObj = (sw) => ({ type: 'sw', pos: '_sw-' + sw.id, sw, runs: sw.runs || {}, qualify_rungs: [] });
const swBmcObj = (sw) => ({ type: 'swbmc', pos: `_sw-${sw.id}.bmc`, sw, runs: sw.runs || {}, qualify_rungs: [], only: BMC_STEPS });

const pad = (n) => String(n).padStart(2, '0');
const span = (top, bot) => (top === bot ? pad(top) : `${pad(top)}–${pad(bot)}`);

async function api(path, body) {
  const r = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let j = {}; try { j = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

const POST_STATES = ['done', 'cur', 'pending', 'fault'];

function stepState(run) {
  if (!run) return 'pending';
  if (run.status === 'running') return 'cur';
  if (run.status === 'interrupted') return 'unknown';
  // the target did not answer: grey, whatever Post's own state (a Firmware row keeps it)
  if (run.verdict === 'unmeasured') return 'unmeasured';
  // Firmware rows carry Post's own step state (needs_update = cur, not a red fault)
  const st = (run.detail || {}).state;
  if (POST_STATES.includes(st)) return st;
  return { pass: 'done', skip: 'skip', fail: 'fault' }[run.verdict] || 'unknown';
}

const gbit = (b) => (b == null ? '?' : (b / 1e9).toFixed(b < 1e10 ? 2 : 1));

function stepNote(run, name) {
  if (!run) return '';
  if (run.status === 'interrupted') return 'interrupted';
  const d = run.detail || {};
  if (run.verdict === 'unmeasured') return d.headline || d.reason || d.error || 'not measured';
  if (d.state && d.headline) return d.headline;
  if (run.verdict === 'fail') return d.error || [d.status, d.note].filter(Boolean).join(' ') || 'fail';
  if (run.verdict === 'skip') return d.reason || 'skipped';
  if (name === 'population-check') return d.profile || '';
  if (name === 'smbios-serial') return `SN ${(d.fru0_serials || [])[0] || '?'}`;
  if (name === 'tpm-state') return `TPM ${d.version || '?'} cleared`;
  if (name === 'console') return d.bytes != null ? `${d.bytes} B` : '';
  if (name === 'install-console') return d.findings ? `${d.findings} finding(s)` : d.findings === 0 ? `clean · ${d.bytes} B` : '';
  if (name === 'iperf-mesh') {
    if (d.served) return `served ${d.served.length}`;
    return ['mgmt', 'A', 'B'].filter((n) => d[n]).map((n) => `${n} ${gbit(d[n].tx_bps)}/${gbit(d[n].rx_bps)}G`).join(' · ');
  }
  if (name === 'fabric-addr') return Object.values(d).filter((x) => typeof x === 'string').join(' ');
  if (name === 'pxe-live' && d.ssh_after_s != null) return `ssh after ${d.ssh_after_s}s`;
  if (d.headline) return d.headline;
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
        ? ioms.map((k, i) => ({ key: d.pos + '-' + k, dev: d, iom: k, idx: i, obj: iomObj(d, k), label: `${d.pos} ${k.toUpperCase()} · ${d.links[k]}` }))
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
  // petite-vue does not re-run the bindings of a tile nested three v-fors deep when
  // only the polled data under it changed: key each group by its content, so a group
  // whose devices changed is re-mounted (and an unchanged one is left alone)
  for (const x of groups) x.key = `${x.range}#${hashStr(JSON.stringify(x.rows.map((r) => r.sw || r.cells.map((c) => c.dev || c.label))))}`;
  return groups;
}

function hashStr(t) {
  let h = 5381;
  for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

function selFromHash() {
  const m = /(?:^#|&)sel=([A-Za-z0-9._-]+)/.exec(location.hash);
  return m ? m[1] : null;
}

function App() {
  return {
    s: { switches: [], devices: [], phases: ['discover', 'firmware', 'qualify', 'deploy', 'done'], profiles: [], rack_runs: {}, events: [] },
    layout: [],                // buildLayout(s), rebuilt on every poll (refresh)
    live: null, sel: selFromHash(), filter: null, q: '', popOpen: null, now: Date.now(),
    actionMsg: null, hdrMsg: null, stepM: null, artM: null, artSeq: 0, closing: false, confirmText: '', closeMsg: null, busy: false,
    qualifying: false, qualText: '', discovering: false, discText: '',
    deploying: false, depText: '', depFrom: 'install', depReinstall: false,
    // per-device Deploy modal: depM = {pos, msg} while open; its inputs are top-level so
    // v-model never reads through a null depM while the modal tears down
    depM: null, devFrom: 'install', devReinText: '', devWaive: {}, devReason: '',
    metricList: [
      { key: 'discover', label: 'discover', title: 'devices in Discover' },
      { key: 'firmware', label: 'firmware', title: 'devices with firmware versions read (report only)' },
      { key: 'qualify', label: 'qualify', title: 'devices in Qualify' },
      { key: 'deploy', label: 'deploy', title: 'devices in Deploy' },
      { key: 'done', label: 'done', title: 'Plan 4' },
      { key: 'alert', label: 'alerts', title: 'devices with a failed rung' },
    ],

    async mounted() {
      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (this.artM) this.backArt(); else if (this.stepM) this.stepM = null; else if (this.depM) this.depM = null; else if (this.deploying) this.deploying = false; else if (this.qualifying) this.qualifying = false; else if (this.discovering) this.discovering = false; else if (this.closing) this.closing = false; else if (this.popOpen) this.popOpen = null; else this.close();
      });
      await this.refresh(); setInterval(() => this.refresh(), POLL_MS);
    },
    async refresh() {
      try { this.s = await api('api/state'); this.layout = buildLayout(this.s); this.live = true; this.now = Date.now(); document.title = `${this.s.prefix} rackpost`; }
      catch (e) { this.live = false; console.error(e); }
    },

    // the selected object: a device (itself), an IOM, a switch or a wedge's BMC
    get selObj() {
      const k = this.sel; if (!k) return null;
      let m = /^_sw-([^.]+)(\.bmc)?$/.exec(k);
      if (m) {
        const sw = (this.s.switches || []).find((x) => x.id === m[1]);
        return !sw ? null : m[2] ? (sw.bmc ? swBmcObj(sw) : null) : swObj(sw);
      }
      m = /^(.+)\.(iom\d+)$/.exec(k);
      if (m) {
        const d = (this.s.devices || []).find((x) => x.pos === m[1]);
        return d && (d.links || {})[m[2]] ? iomObj(d, m[2]) : null;
      }
      return (this.s.devices || []).find((d) => d.pos === k) || null;
    },
    get selKind() { const o = this.selObj; return !o ? null : o.type || 'dev'; },
    get selDev() { return this.selKind === 'dev' ? this.selObj : null; },

    // ---- per-device pipeline (null-safe: petite-vue may evaluate during teardown) ----
    steps(d, phase) {
      if (!d) return {};
      const runs = d.runs || {}, rr = this.s.rack_runs || {};
      const out = {};
      if (phase === 'discover') {
        const steps = (this.s.discover_rungs || []).filter((r) => !d.only || d.only.includes(r));
        for (const rung of steps) out[rung] = runs[rung];
        // a device with only pre-2b Discover rows shows those (order.db history)
        if (!steps.some((r) => runs[r])) {
          for (const rung of this.s.discover_old_rungs || []) if (runs[rung]) out[rung] = runs[rung];
        }
      } else if (phase === 'firmware') {
        for (const rung of this.s.firmware_rungs || []) out[rung] = runs[rung];
        for (const rung of this.s.firmware_old_rungs || []) if (runs[rung]) out[rung] = runs[rung];
      } else if (phase === 'qualify') {
        for (const rung of d.qualify_rungs || []) out[rung] = runs[rung];
      } else if (phase === 'deploy') {
        for (const rung of d.deploy_rungs || []) out[rung] = runs[rung];
      }
      return out;
    },
    stepEntries(d, phase) { return this.entriesOf(this.steps(d, phase), d && d.pos); },
    entriesOf(steps, key) {
      return Object.entries(steps).map(([name, run]) => ({
        name, run, key: `${key}/${name}`, state: stepState(run), note: stepNote(run, name),
        arts: Object.entries((run && run.artifacts) || {}).map(([n, url]) => ({ name: n, url })),
        ev: run && run.detail && (run.detail.rows || run.detail.notes) ? run.detail : null,
        title: (run ? `${run.status}${run.verdict ? ' · ' + run.verdict : ''} · ${run.finished_at || run.started_at}` : 'not run')
          + (run ? ' — ' + stepNote(run, name) : '') + ' (click for the evidence)' }));
    },
    // a JBOD's IOMs: each endpoint's own Firmware rows (key <pos>.iomN)
    endpointFw(d) {
      if (!d) return [];
      return Object.entries(d.endpoint_runs || {}).map(([ep, runs]) => {
        const steps = {};
        for (const rung of this.s.firmware_rungs || []) steps[rung] = runs[rung];
        return { ep, entries: this.entriesOf(steps, `${d.pos}.${ep}`) };
      }).filter((x) => x.entries.some((e) => e.run));
    },
    // the step evidence modal (Post's stepinfo): a snapshot of the step at the click
    openStep(o, phase, st) {
      if (!st) return;
      const run = st.run, d = (run && run.detail) || {};
      this.stepM = {
        who: this.whoName(o), phase, name: st.name, state: st.state,
        word: run && run.verdict && st.state !== 'unmeasured' ? `${STEP_WORD[st.state] || st.state} · ${run.verdict}` : (STEP_WORD[st.state] || st.state),
        headline: run ? (d.headline || st.note || '') : '', rows: d.rows || [], notes: d.notes || [],
        arts: st.arts || [], ran: !!run,
        when: run ? [run.started_at && `started ${run.started_at}`, run.finished_at && `finished ${run.finished_at}`].filter(Boolean).join(' · ') : '' };
    },
    // ---- artifact viewer: the text in-page (first ART_MAX bytes), Copy / Download / Back ----
    // fromStep: Back / Esc return to the evidence modal under it; ✕ / the scrim close both.
    async openArt(a, fromStep) {
      if (!a || !a.url) return;
      const seq = ++this.artSeq;
      this.artM = { name: a.name, url: a.url, fromStep: !!fromStep, loading: true, text: '',
                    error: null, size: null, loaded: 0, lines: 0, truncated: false, copied: '' };
      try {
        const r = await fetch(a.url, { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const total = Number(r.headers.get('Content-Length')) || null;
        const dec = new TextDecoder();
        let text = '', n = 0, cut = false;
        if (r.body && r.body.getReader) {
          const rd = r.body.getReader();
          for (;;) {                                   // stop at the first byte past ART_MAX
            const { done, value } = await rd.read();
            if (done) break;
            const take = Math.min(value.length, ART_MAX - n);
            text += dec.decode(value.subarray(0, take), { stream: true }); n += take;
            if (take < value.length) { cut = true; rd.cancel().catch(() => {}); break; }
          }
          text += dec.decode();
        } else {
          const b = new Uint8Array(await r.arrayBuffer());
          cut = b.length > ART_MAX; n = Math.min(b.length, ART_MAX); text = dec.decode(b.subarray(0, n));
        }
        if (seq !== this.artSeq) return;               // a newer artifact was opened
        const lines = text ? text.split('\n').length - (text.endsWith('\n') ? 1 : 0) : 0;
        this.artM = { ...this.artM, loading: false, text, loaded: n, size: total ?? n, lines, truncated: cut };
      } catch (e) {
        if (seq === this.artSeq && this.artM) this.artM = { ...this.artM, loading: false, error: e.message };
      }
    },
    // Back (and Esc): to the evidence modal it was opened from; close: the viewer and that modal
    backArt() { this.artSeq++; this.artM = null; },
    closeArt() { const f = this.artM && this.artM.fromStep; this.artSeq++; this.artM = null; if (f) this.stepM = null; },
    // a step row's artifact chip: the step's evidence modal with the viewer over it
    openStepArt(o, phase, st, a) { this.openStep(o, phase, st); this.openArt(a, true); },
    artInfo() {
      const m = this.artM; if (!m || m.loading || m.error) return '';
      const kb = (b) => (b == null ? '?' : b < 10240 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KiB` : `${(b / 1048576).toFixed(1)} MiB`);
      return `${m.lines} line${m.lines === 1 ? '' : 's'} · ${kb(m.size)}` + (m.truncated ? ` · first ${kb(m.loaded)} shown` : '');
    },
    // the clipboard API needs a secure context (this page is often plain http): fall back
    async copyArt() {
      const m = this.artM; if (!m || !m.text) return;
      let ok = false;
      try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(m.text); ok = true; } }
      catch (e) { /* fall back below */ }
      if (!ok) {
        const ta = document.createElement('textarea');
        ta.value = m.text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        ta.remove();
      }
      if (this.artM === m) this.artM = { ...m, copied: ok ? (m.truncated ? 'copied (shown part)' : 'copied') : 'copy failed' };
    },
    whoName(o) {
      if (!o) return '';
      return o.type === 'iom' ? `${o.dev.pos} ${o.ep.toUpperCase()}` : o.type === 'sw' ? `SW ${o.sw.id}`
        : o.type === 'swbmc' ? `SW ${o.sw.id} BMC` : o.pos;
    },
    hasRuns(d, phase) { return this.stepEntries(d, phase).some((x) => x.run); },
    // the phase whose steps the tile's second bar shows: the latest one with a run
    curPhase(d) { return this.hasRuns(d, 'deploy') ? 'deploy' : this.hasRuns(d, 'qualify') ? 'qualify' : 'discover'; },
    phaseState(d, p) {
      const v = this.stepEntries(d, p).map((x) => x.state);
      if (v.includes('fault')) return 'fault';
      if (v.length && v.every((x) => x === 'done' || x === 'skip')) return 'done';
      // finished, but a step's target did not answer: grey (not measured), never done
      if (v.includes('unmeasured') && v.every((x) => ['done', 'skip', 'unmeasured'].includes(x))) return 'unmeasured';
      if (v.some((x) => x !== 'pending')) return 'cur';
      return '';
    },
    phaseSegs(d) { return !d ? [] : this.s.phases.map((p) => this.phaseState(d, p)); },
    stepSegs(d) {
      if (!d) return [];
      return this.stepEntries(d, this.curPhase(d)).map((x) => (x.state === 'done' || x.state === 'skip') ? 'done'
        : ['fault', 'cur', 'unmeasured'].includes(x.state) ? x.state : '');
    },
    // a Qualify rung an active Deploy waiver names stays red in the pipeline (the truth)
    // but no longer makes the tile an alert: the operator accepted it, on record
    waived(d, phase, name) { const w = d && d.waiver; return phase === 'qualify' && !!w && w.active && w.rungs.includes(name); },
    faulted(d) {
      return !!d && ['discover', 'firmware', 'qualify', 'deploy'].some((p) =>
        this.stepEntries(d, p).some((x) => x.state === 'fault' && !this.waived(d, p, x.name)));
    },
    phaseKey(d) {
      if (!d) return 'grey';
      if (this.faulted(d)) return 'fault';
      const cp = this.phaseState(d, this.curPhase(d));
      if (cp === 'unmeasured') return 'unmeasured';
      if (this.hasRuns(d, 'deploy')) return cp === 'done' ? 'done' : 'deploy';
      if (this.hasRuns(d, 'qualify')) return cp === 'done' ? 'done' : 'qualify';
      return Object.keys(d.runs || {}).length ? 'discover' : 'grey';
    },
    dotCls(d) { return this.phaseKey(d); },
    phaseDot(d, p) { const st = this.phaseState(d, p); return ['done', 'fault', 'unmeasured'].includes(st) ? st : st === 'cur' ? p : 'grey'; },
    phasePct(d, p) {
      const v = this.stepEntries(d, p); if (!v.length) return '';
      return `${v.filter((x) => x.state === 'done' || x.state === 'skip').length}/${v.length}`;
    },
    stepIcon(st) { return { done: '✓', cur: '◉', fault: '✕', pending: '·', skip: '–', unknown: '?', unmeasured: '○' }[st] || '·'; },
    statusText(d) {
      if (!d) return '';
      for (const p of ['discover', 'firmware', 'qualify', 'deploy']) {
        const bad = this.stepEntries(d, p).find((x) => x.state === 'fault' && !this.waived(d, p, x.name));
        if (bad) return `${p} · ${bad.name}`;
      }
      const p = this.curPhase(d);
      const cur = this.stepEntries(d, p).find((x) => x.state !== 'done' && x.state !== 'skip');
      return !cur ? `${p} ✓` : cur.state === 'unmeasured' ? `${p} · ${cur.name} not measured` : `${p} · ${cur.name}`;
    },
    statusStyle(d) { const c = RGB[this.phaseKey(d)] || RGB.discover; return `background:rgba(${c},.15);color:rgb(${c})`; },
    faults(d) {
      if (!d) return [];
      return ['discover', 'firmware', 'qualify', 'deploy'].flatMap((p) => this.stepEntries(d, p).map((x) => ({ ...x, phase: p })))
        .filter((x) => x.state === 'fault' || x.state === 'unknown')
        .map((x) => ({ rung: x.name, text: x.note, bad: x.state === 'fault', phase: x.phase, st: x }));
    },
    popRun(d) { return d && (d.runs || {})['population-check']; },
    popMark(d) {
      const r = this.popRun(d);
      if (!r || r.status !== 'done') return { t: '·', cls: '', title: 'POP not run yet (Qualify)' };
      const x = r.detail || {};
      if (r.verdict === 'pass') return { t: '✓', cls: 'ok', title: `POP ${x.profile}: every rule found` };
      if (r.verdict === 'skip') return { t: '–', cls: '', title: 'POP skipped: ' + (x.reason || '') };
      if (r.verdict === 'unmeasured') return { t: '○', cls: 'unm', title: 'POP not measured: ' + (x.headline || x.reason || '') };
      return { t: '✕', cls: 'bad', title: 'POP: ' + (x.error || 'fail') };
    },
    openPop(d) { if (this.popRun(d) && this.popRun(d).detail) this.popOpen = d.pos; },
    get popDev() { return this.popOpen ? (this.s.devices.find((d) => d.pos === this.popOpen) || null) : null; },

    // ---- firmware versions (the Firmware stage's reads): equal versions share a colour, like INV ----
    fwOf(d) {
      const parts = ((d && d.fw) || {}).parts || [];
      const cur = (n) => (parts.find((p) => p.part === n) || {}).current;
      return cur('BIOS') || cur('BMC') ? { bios_version: cur('BIOS') || '?', bmc_firmware: cur('BMC') || '?' } : null;
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
      return f ? `BIOS ${f.bios_version} · BMC ${f.bmc_firmware} · Firmware stage (detect only)` : '';
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
               qualify: ds.filter((d) => this.hasRuns(d, 'qualify')).length,
               deploy: ds.filter((d) => this.hasRuns(d, 'deploy')).length, done: 0,
               alert: ds.filter((d) => this.faulted(d)).length };
    },
    toggle(f) { this.filter = this.filter === f ? null : f; },
    inFilter(d, f) {
      if (f === 'alert') return this.faulted(d);
      if (f === 'firmware' || f === 'qualify' || f === 'deploy') return this.hasRuns(d, f);
      return f === 'discover';
    },
    qualifyTitle() {
      const q = this.s.qualify || {}, why = q.why || {};
      const base = 'run Qualify: SOL capture + live boot → battery → population-check, smbios-serial, tpm-state per node, then wedge LLDP → HA path → fabric addresses → iperf mesh';
      const np = (q.not_passed || []).map((p) => (why[p] ? `${p} (${why[p]})` : p));
      return q.passed ? base + ' · rack passed' : np.length ? `${base} · not passed: ${np.join(', ')}` : base;
    },
    stageText() {
      if (this.s.closed_at) return 'closed ' + new Date(this.s.closed_at).toLocaleString();
      if (this.s.stage_running) return `${this.s.stage || 'stage'} ${this.s.cancelling ? 'cancelling' : 'running'}…`;
      const e = (this.s.events || [])[0];
      return e ? `${e.text} · ${new Date(e.ts).toLocaleTimeString()}` : 'no stage run yet';
    },
    // Qualify's lockstep phase: which one, and how many devices wait at its barrier
    get qprog() { return (this.s.qualify || {}).progress || null; },
    qphaseCls(p) {
      const i = (this.s.qualify_phases || []).indexOf(p), n = (this.qprog?.n || 0) - 1;
      return i < n ? 'past' : i === n ? 'cur' : '';
    },
    qphaseTitle() {
      const g = this.qprog || {}, w = g.waiting_on || [];
      return `Qualify phase ${g.n} of ${g.of} (${g.phase}): ${g.done} of ${g.devices} device(s) done, ` +
        `waiting at the barrier` + (w.length ? ` for ${w.join(', ')}` : '');
    },
    // Deploy's lockstep phase (install → firstboot → user → ha-config → ha-test)
    get dprog() { return (this.s.deploy || {}).progress || null; },
    dphaseCls(p) {
      const i = (this.s.deploy_phases || []).indexOf(p), n = (this.dprog?.n || 0) - 1;
      return i < n ? 'past' : i === n ? 'cur' : '';
    },
    dphaseTitle() {
      const g = this.dprog || {}, w = g.waiting_on || [];
      return `Deploy phase ${g.n} of ${g.of} (${g.phase}): ${g.done} of ${g.devices} device(s) done, ` +
        `waiting at the barrier` + (w.length ? ` for ${w.join(', ')}` : '');
    },
    // the order's delivered HA mode (from ha-test)
    get haMode() { return (this.s.deploy || {}).ha_mode || null; },
    haTitle() {
      const h = this.haMode; if (!h) return '';
      return `HA mode delivered: ${h.mode} · ha-test passed on ${h.devices.join(', ')}` +
        (h.missing.length ? ` · not yet: ${h.missing.join(', ')}` : '') + ` · ${h.at}\n` + (h.notes || []).join('\n');
    },
    deployTitle() {
      const q = this.s.deploy || {}, why = q.why || {};
      const base = 'run Deploy: SOL capture + Ubuntu 24.04 install → first boot (installed-OS proof, SOL scan) → flax_user → bond0 active-backup → whole-wedge HA test';
      const gated = this.depHosts().filter((d) => d.deploy_gate).map((d) => `${d.pos} (${d.deploy_gate})`);
      return gated.length ? `${base} · gate: ${gated.join('; ')}` : q.passed ? base + ' · rack deployed' : base;
    },
    eventsTitle() { return (this.s.events || []).slice(0, 12).map((e) => `${e.ts} ${e.kind}: ${e.text}`).join('\n'); },

    // ---- tiles ----
    swKind(sw) { return `${SWKIND[sw.role] || sw.role} · ${sw.model} · ${sw.nos}`; },
    swRun(sw) { return (this.s.rack_runs || {})[sw.role === 'tor' ? 'lldp-' + sw.id : 'switch-fdb']; },
    // the switch's own Discover/Firmware ladder when it ran, else its FDB/LLDP read
    swDot(sw) {
      const o = swObj(sw);
      if (Object.keys(o.runs).length) {
        const k = this.phaseKey(o);
        return k === 'discover' && this.phaseState(o, 'discover') === 'done' ? 'done' : k;
      }
      const r = this.swRun(sw);
      if (!sw.mgmt_ip && !r) return 'grey';
      return !r ? 'grey' : r.status === 'running' ? (sw.role === 'tor' ? 'qualify' : 'discover')
        : r.verdict === 'pass' ? 'done' : r.verdict === 'unmeasured' ? 'unmeasured' : 'fault';
    },
    swObj(sw) { return swObj(sw); },
    swBmcObj(sw) { return swBmcObj(sw); },
    swSel(sw) { return this.sel === '_sw-' + sw.id || this.sel === `_sw-${sw.id}.bmc`; },
    epText(e) { return !e ? '' : `${e.mac || '⋯'} · ${e.ip || '⋯'}`; },
    swTitle(sw) {
      const n = sw.role === 'tor' ? 'lldp-' + sw.id : 'switch-fdb', r = this.swRun(sw);
      return r ? `${n} ${r.verdict || r.status} · ${stepNote(r, n)}` : `${n} not run`;
    },
    cellMac(c) { return c.iom ? (c.obj.view.mac || (c.dev.iom_macs || [])[c.idx]) : c.dev.bmc_mac; },
    // the object a tile stands for (an IOM tile is its own endpoint) and its serial
    cellObj(c) { return c.obj || c.dev; },
    cellSerial(c) { return c.iom ? c.obj.view.serial : c.dev.serial; },
    serialOf(o) {
      if (!o) return null;
      return o.type === 'iom' ? (o.view || {}).serial : o.type === 'sw' || o.type === 'swbmc' ? o.sw.serial : o.serial;
    },
    identOf(o) {
      if (!o) return null;
      return o.type === 'iom' ? (o.view || {}).identity : o.type === 'sw' || o.type === 'swbmc' ? o.sw.identity : o.identity;
    },
    fwViewOf(o) {
      if (!o) return null;
      return o.type === 'iom' ? (o.view || {}).fw : o.type === 'sw' || o.type === 'swbmc' ? o.sw.fw : o.fw;
    },
    // version rows of a bmc-checked read: present parts, then the not-present ones
    verRows(o) {
      const f = this.fwViewOf(o); if (!f) return [];
      return [...(f.parts || []).map((x) => ({ k: x.part, cls: x.phase === 'needs_update' ? 'amb' : '',
                 v: `${x.current}${x.target && x.target !== x.current ? ' → target ' + x.target : ''} · ${x.phase}` })),
              ...Object.entries(f.versions || {}).map(([k, v]) => ({ k, v, cls: '' })),
              ...Object.entries(f.not_present || {}).map(([k, v]) => ({ k, v: `not present (${v})`, cls: 'dimv' }))];
    },
    artList(x) { return Object.entries((x && x.artifacts) || {}).map(([name, url]) => ({ name, url })); },
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
      if (this.sel === this.cellObj(c).pos) cl.push('active');
      if (this.q) cl.push(this.matches(c.dev) ? 'match' : 'faded');
      else if (this.filter && !this.inFilter(c.dev, this.filter)) cl.push('faded');
      return cl.join(' ');
    },
    tileStyle(c) {
      if (c.empty || this.sel !== this.cellObj(c).pos) return '';
      return `background:rgba(${RGB[this.phaseKey(this.cellObj(c))] || RGB.discover},.12)`;
    },
    colName(d) { return !d ? '' : d.col ? (COLNAME[d.col] || `col ${d.col}`) : 'full'; },

    // ---- drawer ----
    open(c) { if (c && !c.empty) this.select(this.cellObj(c).pos); },
    // toggle a drawer by its key (device pos, <pos>.iomN, _sw-<id>, _sw-<id>.bmc)
    select(key) {
      this.actionMsg = null; this.stepM = null; this.artM = null;
      this.sel = this.sel === key ? null : key;
      history.replaceState(null, '', this.sel ? '#sel=' + this.sel : location.pathname);
    },
    close() { this.sel = null; this.stepM = null; this.artM = null; history.replaceState(null, '', location.pathname); },
    // drawer header sub-line per kind
    subLine(o) {
      if (!o) return '';
      if (o.type === 'iom') {
        const d = o.dev;
        return `OU ${d.ou} · ${d.pos} ${o.ep.toUpperCase()} · ${(d.names || {})[o.ep] || ''} · ${d.kind} ${d.part} · link ${(d.links || {})[o.ep] || '—'}`;
      }
      if (o.type === 'sw' || o.type === 'swbmc') {
        const sw = o.sw, sp = sw.site_port || {};
        return `OU ${sw.ou} · SW ${sw.id}${o.type === 'swbmc' ? ' BMC' : ''} · ${this.swKind(sw)}` +
          (sp.switch ? ` · site ${sp.switch} ${sp.port}` : '');
      }
      return `OU ${o.ou} ${this.colName(o)} · ${o.pos} · ${(o.names || {}).host || (o.names || {}).host1 || (o.names || {}).iom1 || ''} · ${o.kind} ${o.part}`;
    },
    // the IOM's own link row (switch:port, seen MAC, oper from its switchportlink)
    iomLink(o) {
      if (!o || o.type !== 'iom') return null;
      const seen = (o.dev.link_seen || {})[o.ep] || {};
      return { name: o.ep, expected: (o.dev.links || {})[o.ep], seen: seen.text || 'not run', state: seen.state || 'dimv' };
    },
    // a switch's address rows: SONiC mgmt, its BMC (wedges: opens the BMC drawer)
    swAddr(o) {
      if (!o || !o.sw) return [];
      const sw = o.sw, out = [];
      if (sw.sonic && o.type === 'sw') out.push({ role: 'SONIC', ...sw.sonic, key: null, note: sw.mgmt_ip && !String(sw.mgmt_ip).startsWith('fake-') ? `geometry mgmt_ip ${sw.mgmt_ip}` : 'dynamic lease (vid 26 pool)' });
      if (sw.bmc) out.push({ role: 'BMC', ...sw.bmc, key: o.type === 'sw' ? `_sw-${sw.id}.bmc` : null, note: 'dynamic lease (vid 26 pool)' });
      return out;
    },
    // identity facts beside the serial (whatever the kind's read carried)
    // (facts the read's own rows already show are not repeated)
    identRows(o) {
      const i = this.identOf(o); if (!i) return [];
      const shown = new Set((i.rows || []).map((r) => String(r[1]).split(' (')[0].toLowerCase()));
      const L = { product_name: 'product', board_product: 'board product',
                  board_serial: 'board serial', part_number: 'part number', service_tag: 'service tag',
                  asset_tag: 'asset tag', local_mac: 'local MAC (BMC)', ext_mac_base: 'ext MAC base',
                  base_mac: 'base MAC', syseeprom_serial: 'SONiC syseeprom serial', platform: 'platform' };
      return Object.keys(L).filter((k) => i[k] && !shown.has(String(i[k]).toLowerCase()))
        .map((k) => ({ k: L[k], v: i[k] }));
    },
    fwTarget(o) { return !o ? null : o.type === 'iom' ? o.dev.pos : o.sw ? '_sw-' + o.sw.id : o.pos; },
    // the identity read's own rows, minus the versions the versions section shows
    identReadRows(o) {
      const i = this.identOf(o); if (!i) return [];
      const v = new Set(this.verRows(o).map((r) => r.k));
      return (i.rows || []).filter((r) => !v.has(r[0]));
    },
    portRows(o) { return (o && o.sw && o.type === 'sw' && o.sw.ports) || []; },
    addrRows(d) {
      if (!d) return [];
      const rr = (d.runs || {})['bmc-reserved'] || (d.runs || {}).reserve;
      const reserved = new Set(((rr || {}).detail?.reserved || []).map((x) => x.role));
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
    // Discover may power on / cycle nodes not in the live image: the rack-wide run
    // takes the typed order id (like Qualify); a drawer RETRY a plain confirm naming it.
    discHosts() { return (this.s.devices || []).filter((d) => (d.qualify_rungs || []).length); },
    openDiscover() { this.discovering = true; this.discText = ''; },
    async doDiscover() {
      this.discovering = false;
      try { await api('api/stage/discover', { confirm: this.discText }); this.hdrMsg = 'discover started'; }
      catch (e) { this.hdrMsg = 'discover: ' + e.message; }
      await this.refresh();
    },
    async firmware(pos) {
      try {
        await api('api/stage/firmware', pos ? { positions: [pos] } : {});
        const m = `firmware started${pos ? ' for ' + pos : ''} (detect only)`;
        if (pos) this.actionMsg = m; else this.hdrMsg = m;
      } catch (e) { const m = 'firmware: ' + e.message; if (pos) this.actionMsg = m; else this.hdrMsg = m; }
      await this.refresh();
    },
    async retry(pos) {
      if (!pos) return;
      if (!window.confirm(`Re-run Discover for ${pos} only? A node that is on but not in the live ` +
                          'image is power-cycled into it.')) return;
      try { await api('api/stage/discover', { positions: [pos], confirm: true }); this.actionMsg = `discover started for ${pos}`; }
      catch (e) { this.actionMsg = 'discover: ' + e.message; }
      await this.refresh();
    },
    // Cancel: the running stage stops between steps/phases (a power action already
    // sent finishes); later steps are recorded skip "cancelled".
    async cancelStage() {
      if (!window.confirm(`Cancel the running ${this.s.stage}? It stops after the steps now running.`)) return;
      try { await api('api/stage/cancel', { confirm: true, stage: this.s.stage }); this.hdrMsg = `${this.s.stage} cancelling…`; }
      catch (e) { this.hdrMsg = 'cancel: ' + e.message; }
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
    // ---- Deploy: wipes disks. Rack-wide = typed order id (+ reinstall of installed nodes
    // with the same typed id); a drawer DEPLOY = its own confirm, waivers and reason, and a
    // reinstall there takes the typed order id too ----
    depHosts() { return (this.s.devices || []).filter((d) => (d.deploy_rungs || []).length); },
    installed(d) { return !!d && ((d.runs || {}).install || {}).verdict === 'pass'; },
    depInstalled() { return this.depHosts().filter((d) => this.installed(d)).map((d) => d.pos); },
    openDeploy() { this.deploying = true; this.depText = ''; this.depFrom = 'install'; this.depReinstall = false; },
    async doDeploy() {
      this.deploying = false;
      const body = { confirm: this.depText, from: this.depFrom };
      if (this.depFrom === 'install' && this.depReinstall) body.reinstall = this.depText;
      try { await api('api/stage/deploy', body); this.hdrMsg = 'deploy started'; }
      catch (e) { this.hdrMsg = 'deploy: ' + e.message; }
      await this.refresh();
    },
    openDeployDev(d) {
      if (!d) return;
      this.devFrom = this.installed(d) ? 'firstboot' : 'install'; this.devReinText = ''; this.devReason = '';
      this.devWaive = Object.fromEntries((d.gate_blockers || []).filter((r) => !r.startsWith('verdict ')).map((r) => [r, false]));
      this.depM = { pos: d.pos, msg: null };
    },
    get depDev() { return this.depM ? (this.s.devices || []).find((d) => d.pos === this.depM.pos) || null : null; },
    depWaived() { return Object.keys(this.devWaive || {}).filter((r) => this.devWaive[r]); },
    // what still blocks the device with the ticked waivers (empty = it may enter Deploy)
    depLeft() { const d = this.depDev; return !d ? [] : (d.gate_blockers || []).filter((r) => !this.depWaived().includes(r)); },
    depReady() {
      if (!this.depM || this.depLeft().length) return false;
      if (this.depWaived().length && !this.devReason.trim()) return false;
      // a reinstall wipes disks: the typed order id, like the rack-wide modal
      return !(this.devFrom === 'install' && this.installed(this.depDev) && this.devReinText !== this.s.order);
    },
    async doDeployDev() {
      const m = this.depM; if (!m) return;
      const body = { positions: [m.pos], confirm: true, from: this.devFrom };
      if (this.devFrom === 'install' && this.installed(this.depDev)) body.reinstall = this.devReinText;
      const w = this.depWaived();
      if (w.length) body.waive = { [m.pos]: { rungs: w, reason: this.devReason.trim() } };
      try { await api('api/stage/deploy', body); this.depM = null; this.actionMsg = `deploy started for ${m.pos}`; }
      catch (e) { this.depM = { ...m, msg: e.message }; }
      await this.refresh();
    },
    waiverChip(d) {
      const w = d && d.waiver; if (!w) return null;
      return { t: w.active ? 'waived' : 'waived (now passed)', cls: w.active ? 'amb' : 'dimv',
               title: `Deploy gate waiver: ${w.rungs.join(', ')} · ${w.reason} · by ${w.who} · ${w.at}` };
    },
    openInv(d) {
      const a = ((d && (d.runs || {}).inventory) || {}).artifacts || {};
      const n = a['macinv-v'] ? 'macinv-v' : 'macinv';
      if (a[n]) this.openArt({ name: n, url: a[n] });
    },
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
