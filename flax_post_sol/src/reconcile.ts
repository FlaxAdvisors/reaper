/* src/reconcile.ts
 *
 * Drives session lifecycle from per-slot BMC-reachability feeds, not from
 * client connects and not from node power: the post viewer's /api/v1/blades
 * (POST_BLADES_URL) and, when RACKPOST_SOL_TARGETS_URL is set, rackpost's
 * /rackpost/api/sol-targets (the host BMCs of every enabled customer rack, same
 * {slots: [...]} shape, ports "rackpost:<prefix>:<pos>"). The feeds are unioned
 * per tick; one that fails is skipped for that tick, the others still act (the
 * skip is logged once, then at most every FEED_WARN_EVERY_MS while it lasts).
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
 * Exception: a feed listed in `endOnLeave` (rackpost's) is a list of targets,
 * not of hardware: when it ANSWERS without a port it started, LEAVE_TICKS times
 * in a row (the rack was closed or disabled; one odd answer is not enough), that
 * port's session is stopped (history kept).
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
  /** the feed url it came from (set by fetchAllReach) */
  feed?: string;
}

// answering ticks a target must be missing from its feed before its session ends
export const LEAVE_TICKS = Number(process.env.POST_SOL_LEAVE_TICKS ?? 4);
// a failing feed is logged when it starts failing, then at most this often
export const FEED_WARN_EVERY_MS = Number(process.env.POST_SOL_FEED_WARN_SECS ?? 600) * 1000;

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

export interface Reach {
  /** union of every feed that answered, in url order, each tagged with its feed */
  blades: BladeReach[];
  answered: Set<string>;
  /** url -> why it did not answer this tick */
  errors: Map<string, string>;
}

/** Every feed, never throwing: one service being down (rackpost restarting)
 *  never pauses the other's. */
export async function fetchAllReach(
  urls: string[],
  fetchFn: typeof fetch = fetch
): Promise<Reach> {
  const got = await Promise.allSettled(urls.map((u) => fetchBladeReach(u, fetchFn)));
  const r: Reach = { blades: [], answered: new Set(), errors: new Map() };
  got.forEach((g, i) => {
    if (g.status === "fulfilled") {
      r.answered.add(urls[i]);
      r.blades.push(...g.value.map((b) => ({ ...b, feed: urls[i] })));
    } else r.errors.set(urls[i], String(g.reason));
  });
  return r;
}

/** Rate-limited feed failure logging: the first failure, then a reminder at
 *  most every `everyMs` while it lasts, and one line when the feed is back. */
export function makeFeedWarn(everyMs: number = FEED_WARN_EVERY_MS) {
  const failing = new Map<string, { since: number; last: number; n: number }>();
  return (r: Reach, now: number): void => {
    for (const [url, err] of r.errors) {
      const f = failing.get(url);
      if (!f) {
        failing.set(url, { since: now, last: now, n: 1 });
        logger.warn(`[reconcile] feed skipped: ${url}: ${err} (repeats logged every ${Math.round(everyMs / 1000)} s)`);
        continue;
      }
      f.n += 1;
      if (now - f.last >= everyMs) {
        f.last = now;
        logger.warn(`[reconcile] feed still skipped (${f.n} ticks, ${Math.round((now - f.since) / 1000)} s): ${url}: ${err}`);
      }
    }
    for (const url of r.answered) {
      const f = failing.get(url);
      if (f) {
        failing.delete(url);
        logger.info(`[reconcile] feed answers again after ${f.n} skipped tick(s): ${url}`);
      }
    }
  };
}

export interface ReconcileDeps {
  bladesUrls: string[];
  /** a live session exists for this ip. */
  hasSession: (ip: string) => boolean;
  start: (ip: string) => void;
  replace: (oldIp: string, newIp: string) => void;
  /** stop a session (history kept): a port that left an endOnLeave feed */
  stop?: (ip: string) => void;
  /** feeds whose ports are targets, not hardware (rackpost's sol-targets) */
  endOnLeave?: string[];
  fetchFn?: typeof fetch;
  now?: () => number;
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
  const portState = new Map<string, { mac: string; ip: string; feed?: string; gone?: number }>();
  const feedWarn = makeFeedWarn();

  async function tick(): Promise<void> {
    const r = await fetchAllReach(deps.bladesUrls, deps.fetchFn);
    feedWarn(r, (deps.now ?? Date.now)());
    if (!r.answered.size) return; // every feed failed: pause, keep every session
    for (const b of r.blades) {
      if (!b.reachable) continue; // only reachable BMCs act; keep-on-loss otherwise
      const prev = portState.get(b.port);
      if (!prev) {
        if (!deps.hasSession(b.ip)) deps.start(b.ip);
        portState.set(b.port, { mac: b.mac, ip: b.ip, feed: b.feed });
      } else if (prev.mac !== b.mac) {
        deps.replace(prev.ip, b.ip); // device swapped at this slot
        portState.set(b.port, { mac: b.mac, ip: b.ip, feed: b.feed });
      } else if (!deps.hasSession(b.ip)) {
        deps.start(b.ip); // same device, recover missing session (server restart/crash)
      }
    }
    // unreachable/vanished blades: no action (keep-on-loss); portState retained
    // so the eventual replacement (a new reachable mac) is still detected.
    // A target feed that answered without a port it started, LEAVE_TICKS times in a
    // row: that target is gone (rack closed/disabled), so its session ends -- unless
    // another listed port still uses the same BMC ip.
    const endOnLeave = new Set(deps.endOnLeave ?? []);
    const listed = new Set(r.blades.map((b) => b.port));
    const ips = new Set(r.blades.map((b) => b.ip));
    for (const [port, st] of portState) {
      if (!st.feed || !endOnLeave.has(st.feed) || !r.answered.has(st.feed)) continue;
      if (listed.has(port)) { st.gone = 0; continue; }
      st.gone = (st.gone ?? 0) + 1;
      if (st.gone < LEAVE_TICKS) continue;
      portState.delete(port);
      if (ips.has(st.ip)) continue;
      logger.info(`[reconcile] ${port} left ${st.feed}: ending the session to ${st.ip}`);
      deps.stop?.(st.ip);
    }
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
