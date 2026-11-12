// flax_post/web/static/sol.js — imperative SOL console controller.
// petite-vue can't drive xterm.js reactively (it owns its own canvas/DOM),
// so this is a plain closure exposing window.SolConsole; app.js calls it
// from openModal('sol')/closeModal() around the petite-vue-rendered refs.
//
// No reconnect-on-power-on logic here: the SOL server (flax_post_sol,
// Task 2) wires lock handlers for pending clients too, so a plain
// connect-on-open / disconnect-on-close socket already gets a working
// lock the moment its blade powers on.
window.SolConsole = (function () {
  let term = null, socket = null, held = false, termEl = null, connIp = null, lastData = null;

  function setHeld(v, termElRef) { held = v; if (termElRef) termElRef.classList.toggle('disabled', !v); }

  return {
    isHolder: () => held,
    // the BMC ip the live socket is connected to (null when closed) — lets the
    // Relaunch button detect a re-address / new device at the slot and reconnect.
    currentIp: () => connIp,
    // seconds since the last console byte arrived on this socket (null before any)
    idleSeconds: () => (lastData === null ? null : Math.round((Date.now() - lastData) / 1000)),

    // cb: { onLock(status, held), onEvent(msg), onClient(socketId) }
    open(el, statusEl, bmcIp, cb) {
      this.close();
      termEl = el;
      connIp = bmcIp;
      cb = cb || {};
      const pushEvent = (msg) => { cb.onEvent && cb.onEvent(new Date().toLocaleTimeString() + '  ' + msg); };

      term = new Terminal({
        cols: 132, rows: 28, convertEol: true, cursorBlink: false,
        fontFamily: 'monospace', fontSize: 14,
        theme: { background: '#0d1117', foreground: '#d4d4d4', cursor: '#d4d4d4' },
      });
      term.open(el);
      term.options.disableStdin = true;
      setHeld(false, termEl);
      term.write('Connecting…\r\n');

      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      socket = io(`${proto}${location.host}/sol/${bmcIp}`, { transports: ['websocket'],
        // never give up: a server restart longer than 5 quick attempts used to
        // leave the modal permanently dead until reopened (2026-09-11)
        reconnection: true, reconnectionAttempts: Infinity, reconnectionDelay: 1000, reconnectionDelayMax: 10000 });
      if (socket.io && socket.io.on) socket.io.on('reconnect', (n) => pushEvent('reconnected after ' + n + ' attempt(s); history replayed'));

      socket.on('connect', () => { pushEvent('connected'); cb.onClient && cb.onClient(socket.id); });
      socket.on('disconnect', (r) => {
        pushEvent('disconnected: ' + r);
        // reset local holder state — the server-side lock is gone with the
        // socket, so the UI must not keep showing "Release Lock" for a
        // connection that no longer exists (plain snippet omitted this).
        setHeld(false, termEl);
        if (term) { term.options.disableStdin = true; term.options.cursorBlink = false; }
        cb.onLock && cb.onLock(null, false);
      });
      socket.on('connect_error', (e) => pushEvent('connect_error: ' + e.message));

      // a history replay is the whole retained buffer: start from a clean screen
      socket.on('terminal:history', (d) => { if (term) { term.reset(); term.write(d); } lastData = Date.now(); pushEvent('terminal:history received'); });
      socket.on('terminal:data', (d) => { lastData = Date.now(); term && term.write(d); });

      socket.on('sol:status', (s) => pushEvent('sol:status ' + (s.state === 'off' ? 'powered off — no active capture'
        : s.state === 'ended' ? 'session ended (powered off) — showing last capture' : s.state)));

      socket.on('lock:status', (m) => {
        const now = !!(m.holder && socket && m.holder === socket.id);
        setHeld(now, termEl);
        if (term) { term.options.disableStdin = !now; term.options.cursorBlink = now; now ? term.focus() : term.blur(); }
        pushEvent('lock:status holder=' + (m.holder || 'none'));
        cb.onLock && cb.onLock(m.holder || null, now);
      });

      term.onData((d) => { if (socket && socket.connected && held) socket.emit('terminal:input', d); });
    },

    lock() { socket && socket.emit('lock:request'); },
    unlock() { socket && socket.emit('lock:release'); },
    requestRelease() { socket && socket.emit('lock:request_release'); },
    relaunch() { socket && socket.emit('sol:relaunch'); },

    close() {
      if (socket) { socket.disconnect(); socket = null; }
      if (term) { term.dispose(); term = null; }
      held = false; termEl = null; connIp = null; lastData = null;
    },
  };
})();
