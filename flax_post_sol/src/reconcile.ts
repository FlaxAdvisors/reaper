/* src/reconcile.ts
 *
 * Drives session lifecycle from the post viewer's per-slot BMC-reachability
 * feed (/api/v1/blades), not from client connects and not from node power.
 * A BMC is powered on AC even when its node is off, so starting SOL as soon
 * as the BMC is reachable means `sol activate` is already listening and
 * captures POST from byte one. Sessions are ONLY created/destroyed here; a
 * client socket connecting to /sol/<ip> never spawns or tears one down (see
 * io/handlers/terminal.ts).
 *
 * reachable = bmc_ip set AND bmc_pinged. A persistent portState map tracks
 * which physical device (bmc_mac) currently owns each slot's session so a
 * device swap (new bmc_mac at the same, port-deterministic, slot IP) can be
 * detected and relaunched — see makeReconciler below.
 *
 * Keep-on-loss: a blade going power-off, BMC-unreachable, or vanishing from
 * the feed triggers NO teardown — the session and its ring buffer persist so
 * the operator can read the console around a disappearance/crash. Only an
 * explicit, reachable device replacement (bmc_mac change) tears down/relaunches.
 */
import { logger } from "./utils/logger";

// The ip is written verbatim into a bash pty as `soltriage ${ip}`. It comes
// from the trusted, loopback-only viewer, but we still refuse anything that
// isn't purely digits and dots so a malformed/hostile bmc_ip can never inject
// shell metacharacters.
const IP_SHELL_SAFE = /^[0-9.]+$/;

export interface BladeReach {
  port: string;
  mac: string;
  ip: string;
  reachable: boolean;
}

interface BladesResponseSlot {
  port?: string;
  bmc_mac?: string | null;
  bmc_ip?: string | null;
  bmc_pinged?: boolean;
  empty?: boolean;
}

interface BladesResponse {
  slots?: BladesResponseSlot[];
}

export async function fetchBladeReach(
  bladesUrl: string,
  fetchFn: typeof fetch = fetch
): Promise<BladeReach[]> {
  const res = await fetchFn(bladesUrl);
  if (!res.ok) {
    throw new Error(`blades fetch failed: ${bladesUrl} -> ${res.status}`);
  }
  const body = (await res.json()) as BladesResponse;
  const out: BladeReach[] = [];
  for (const s of body.slots ?? []) {
    if (!s || s.empty || !s.bmc_ip || !s.bmc_mac) continue;
    if (!IP_SHELL_SAFE.test(String(s.bmc_ip))) continue;
    out.push({
      port: s.port as string,
      mac: String(s.bmc_mac),
      ip: String(s.bmc_ip),
      reachable: !!s.bmc_pinged,
    });
  }
  return out;
}

export interface ReconcileDeps {
  bladesUrl: string;
  /** a live session exists for this ip. */
  hasSession: (ip: string) => boolean;
  start: (ip: string) => void;
  replace: (oldIp: string, newIp: string) => void;
  fetchFn?: typeof fetch;
}

/**
 * Builds a reconciler with its own persistent portState (device identity per
 * slot). One reconciler per process — call tick() on an interval.
 *
 * On a failed/erroring blades fetch, tick() PAUSES: it logs and returns
 * without touching existing sessions or portState. A transient viewer blip
 * must never tear down a live SOL capture.
 */
export function makeReconciler(deps: ReconcileDeps) {
  const portState = new Map<string, { mac: string; ip: string }>();

  async function tick(): Promise<void> {
    let blades: BladeReach[];
    try {
      blades = await fetchBladeReach(deps.bladesUrl, deps.fetchFn);
    } catch (e) {
      logger.warn(`[reconcile] blades fetch failed, keeping sessions: ${e}`);
      return;
    }
    for (const b of blades) {
      if (!b.reachable) continue; // only reachable BMCs act; keep-on-loss otherwise
      const prev = portState.get(b.port);
      if (!prev) {
        if (!deps.hasSession(b.ip)) deps.start(b.ip);
        portState.set(b.port, { mac: b.mac, ip: b.ip });
      } else if (prev.mac !== b.mac) {
        deps.replace(prev.ip, b.ip); // device swapped at this slot
        portState.set(b.port, { mac: b.mac, ip: b.ip });
      } else if (!deps.hasSession(b.ip)) {
        deps.start(b.ip); // same device, recover missing session (server restart/crash)
      }
    }
    // unreachable/vanished blades: no action (keep-on-loss); portState retained
    // so the eventual replacement (a new reachable mac) is still detected.
  }

  return { tick };
}

export function startReconcileLoop(
  deps: ReconcileDeps,
  intervalMs: number
): NodeJS.Timeout {
  const r = makeReconciler(deps);
  void r.tick();
  const timer = setInterval(() => void r.tick(), intervalMs);
  // Don't let the reconcile timer keep the process alive on its own (tests /
  // graceful shutdown); the http server is what actually holds the process open.
  timer.unref?.();
  return timer;
}
