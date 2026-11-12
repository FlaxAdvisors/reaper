/* back/src/sessions/manager.ts
 *
 * Session lifecycle is reconcile-driven (see ../reconcile.ts): only
 * startSession/stopSession create or tear down the pty. A client socket
 * connecting to /sol/<ip> attaches to whatever is already here (or waits as
 * a pending client) — it never spawns a session itself.
 */
import * as pty from "node-pty";
import { IPty } from "node-pty";
import { Server } from "socket.io";
import { logger } from "../utils/logger";
import { appendCapture } from "./capture";
import { LockManager } from "./lock";
import { Session } from "./types";

// How much captured output to retain per session, in characters (the buffer
// is chunk-of-string based, so this is an approximation of bytes for
// single-byte terminal output). Retained across power-off; only ever reset
// by the next startSession (power-on) — no TTL/timer eviction.
const HISTORY_BUFFER_SIZE_CHARS = Number(process.env.POST_SOL_HISTORY_BYTES ?? 512 * 1024);

const sessions = new Map<string, Session>();

// ipmitool prints these into the pty when the BMC-side SOL payload dies; the
// pty itself stays alive, so the reconcile loop's pty-exit recovery never
// fires and the console just goes quiet (2026-09-11: every operator-powered
// blade needed the Relaunch button). Match on a rolling tail of the stream and
// relaunch after a short delay, at most once per cooldown per ip.
export const SOL_DROP_RE = /SOL session closed by BMC|Unable to establish IPMI v2 \/ RMCP\+ session|No response (de)?activating SOL payload|SOL payload already active|Error sending SOL data|Session already ended|Error: Unable to (read|send)/i;
const AUTO_RELAUNCH_COOLDOWN_MS = Number(process.env.POST_SOL_AUTO_RELAUNCH_COOLDOWN_MS ?? 30000);
const AUTO_RELAUNCH_DELAY_MS = 2000;

/** Record a pty chunk; when it carries an ipmitool drop message, schedule a
 *  relaunch. Returns true when a relaunch was scheduled. */
export function noteSolDrop(session: Session, data: string, now: number = Date.now()): boolean {
    session.lastDataAt = now;
    session.tailText = ((session.tailText ?? "") + data).slice(-512);
    if (!SOL_DROP_RE.test(session.tailText)) return false;
    session.tailText = "";
    if (now - (session.lastAutoRelaunch ?? 0) < AUTO_RELAUNCH_COOLDOWN_MS) return false;
    session.lastAutoRelaunch = now;
    logger.warn(`[${session.ip}] SOL drop message in stream; relaunching in ${AUTO_RELAUNCH_DELAY_MS}ms`);
    setTimeout(() => {
        try { relaunchSession(session.ip); }
        catch (error) { logger.error(`[${session.ip}] auto relaunch failed: ${error instanceof Error ? error.message : String(error)}`); }
    }, AUTO_RELAUNCH_DELAY_MS);
    return true;
}

// Sockets that connected to /sol/<ip> before a live session existed for that
// ip. Moved into session.clients the next time startSession(ip) runs.
const pendingClients = new Map<string, Set<string>>();

let ioRef: Server | undefined;

/** Called once from index.ts so startSession/stopSession can push status/data to clients. */
export function setIo(io: Server) {
    ioRef = io;
}

export function createSession(ip: string, ptyProcess: IPty | null): Session {
    const session: Session = {
        ip,
        ptyProcess,
        clients: new Set(),
        lock: {
            holder: null,
            requestedBy: new Set(),
        },
        lockManager: new LockManager(),
        historyBuffer: [],
        state: "ended",
    };
    sessions.set(ip, session);
    return session;
}

export function attachPtyEvents(io: Server, session: Session) {
    const { ip, ptyProcess } = session;
    if (!ptyProcess) return;

    ptyProcess.onData((data: string) => {
        try {
            appendCapture(session.ip, data);
            noteSolDrop(session, data);
            session.historyBuffer.push(data);

            // Calculate current size
            let currentSize = 0;
            for (const chunk of session.historyBuffer) {
                currentSize += chunk.length;
            }

            // Trim the history buffer if it exceeds the size limit
            while (currentSize > HISTORY_BUFFER_SIZE_CHARS && session.historyBuffer.length > 0) {
                const removedChunk = session.historyBuffer.shift(); // Remove the oldest chunk
                if (removedChunk) {
                    currentSize -= removedChunk.length;
                }
            }

            for (const clientId of session.clients.keys()) {
                io.of(`/sol/${session.ip}`).to(clientId).emit("terminal:data", data);
            }
        } catch (error) {
            if (error instanceof Error) {
                logger.error(`[${ip}] Error emitting terminal data: ${error.message}`);
            } else {
                logger.error(`[${ip}] Unknown error emitting terminal data: ${String(error)}`);
            }
        }
    });

    ptyProcess.onExit(({ exitCode = 0, signal = null }) => {
        try {
            logger.info(`[${session.ip}] PTY exited with code ${exitCode} (signal: ${signal})`);
            // Only act if the exiting pty is STILL the session's current pty. A
            // relaunch/replace kills the old pty and spawns a fresh one; the old
            // pty's late onExit must not clobber that fresh session (the guard
            // below). When it IS current, the exit was unexpected (soltriage /
            // ipmitool / bash died) — flip to "ended" so hasLiveSession() reads
            // false and the reconcile loop's recover branch restarts it next
            // tick IF the BMC is still reachable (keep-on-loss otherwise).
            if (session.ptyProcess === ptyProcess) {
                session.state = "ended";
                session.ptyProcess = null;
            }
        } catch (error) {
            if (error instanceof Error) {
                logger.error(`[${ip}] Error handling PTY exit: ${error.message}`);
            } else {
                logger.error(`[${ip}] Unknown error handling PTY exit: ${String(error)}`);
            }
        }
    });
}

function spawnBashPty(): IPty {
    return pty.spawn("bash", [], {
        name: "xterm-color",
        cols: 132,
        rows: 32,
        cwd: process.env.HOME,
        env: process.env,
    });
}

export function getSession(ip: string): Session | undefined {
    return sessions.get(ip);
}

/** ips whose session is currently live (pty spawned) — the reconcile loop's "running" set. */
export function runningIps(): Set<string> {
    return new Set([...sessions].filter(([, s]) => s.state === "live").map(([ip]) => ip));
}

/** Whether a live session exists for ip — the reconcile loop's per-ip hasSession check. */
export function hasLiveSession(ip: string): boolean {
    return sessions.get(ip)?.state === "live";
}

/**
 * Power-on: (re)spawn the pty and drive soltriage against ip. If a session
 * already exists (blade was previously live or has since been stopped),
 * reuse it in place — reset the retained buffer and release any stale lock
 * from the prior boot, but keep the same clients/session identity.
 */
export function startSession(ip: string): Session {
    let session = sessions.get(ip);
    if (session) {
        session.historyBuffer = [];
        session.lockManager = new LockManager(); // a lock only makes sense within one boot
    } else {
        session = createSession(ip, null);
    }

    const ptyProcess = spawnBashPty();
    session.ptyProcess = ptyProcess;
    session.state = "live";

    if (ioRef) attachPtyEvents(ioRef, session);
    ptyProcess.write(`soltriage ${ip}\r`);

    const pending = pendingClients.get(ip);
    if (pending) {
        for (const clientId of pending) session.clients.add(clientId);
        pendingClients.delete(ip);
    }

    if (ioRef) {
        ioRef.of(`/sol/${ip}`).emit("client_count_update", session.clients.size);
        ioRef.of(`/sol/${ip}`).emit("sol:status", { state: "live" });
        // The LockManager was replaced above; tell every client the lock is now
        // free so a holder from the prior boot stops showing a stale "you hold
        // the lock" UI over a terminal whose input gate reads the new manager.
        ioRef.of(`/sol/${ip}`).emit("lock:status", { holder: null, isHeld: false });
    }
    logger.info(`[${ip}] Session started (power-on)`);
    return session;
}

/**
 * Power-off: kill the pty (frees the held IPMI/SOL session on the BMC) but
 * KEEP historyBuffer for viewing — it is only ever cleared by the next
 * startSession for this same ip. No TTL/timer clears it.
 */
export function stopSession(ip: string): void {
    const session = sessions.get(ip);
    if (!session) return;

    try {
        session.ptyProcess?.kill();
    } catch (error) {
        if (error instanceof Error) {
            logger.error(`[${ip}] Error killing PTY on stop: ${error.message}`);
        } else {
            logger.error(`[${ip}] Unknown error killing PTY on stop: ${String(error)}`);
        }
    }

    session.ptyProcess = null;
    session.state = "ended";
    session.lockManager = new LockManager(); // release any held lock — nothing to type into anymore

    if (ioRef) {
        ioRef.of(`/sol/${ip}`).emit("sol:status", { state: "ended" });
        // Lock was released (fresh manager); sync every client's lock UI.
        ioRef.of(`/sol/${ip}`).emit("lock:status", { holder: null, isHeld: false });
    }
    logger.info(`[${ip}] Session stopped (power-off); history retained`);
}

/**
 * Manual recovery (socket event sol:relaunch): kill + respawn soltriage for
 * this ip, resetting the buffer and the write-lock. Used to recover a
 * wedged/lost SOL session and to break an abandoned lock hold.
 */
export function relaunchSession(ip: string): Session {
    const session = sessions.get(ip);
    if (session) {
        try {
            session.ptyProcess?.kill();
        } catch (error) {
            if (error instanceof Error) {
                logger.error(`[${ip}] Error killing PTY on relaunch: ${error.message}`);
            } else {
                logger.error(`[${ip}] Unknown error killing PTY on relaunch: ${String(error)}`);
            }
        }
        session.ptyProcess = null;
    }
    logger.info(`[${ip}] Session relaunch requested`);
    return startSession(ip); // resets buffer + lockManager (+ broadcasts lock:status) + respawns
}

/**
 * Device replacement (reconcile loop, on a new reachable bmc_mac at a slot):
 * a different physical BMC now occupies the slot's IP. If the slot's IP
 * actually changed, stop the old ip's session first (its buffer is retained,
 * viewable via keep-on-loss, at the old ip) before starting fresh at the new
 * ip. Either way the new ip gets a fresh buffer + lock via relaunchSession.
 */
export function replaceSession(oldIp: string, newIp: string): void {
    if (oldIp !== newIp) stopSession(oldIp); // retains old buffer at old ip
    relaunchSession(newIp);
}

/** Attach-only connect path (io/handlers/terminal.ts) for an ip with no live/ended session yet. */
export function addPendingClient(ip: string, clientId: string) {
    let set = pendingClients.get(ip);
    if (!set) {
        set = new Set();
        pendingClients.set(ip, set);
    }
    set.add(clientId);
}

export function removePendingClient(ip: string, clientId: string) {
    pendingClients.get(ip)?.delete(clientId);
}

export { sessions };
