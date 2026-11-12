import { describe, it, expect, beforeEach, vi } from "vitest";

// node-pty is native + spawns a real shell; mock it so startSession/stopSession
// exercise the lifecycle without a real pty.
vi.mock("node-pty", () => ({
  spawn: () => ({ onData: vi.fn(), onExit: vi.fn(), write: vi.fn(), kill: vi.fn() }),
}));

import { applyLockHandlers } from "./lock";
import { startSession, stopSession, getSession, sessions } from "../../sessions/manager";

// Minimal socket.io Server stub — applyLockHandlers only uses io.of(nsp).emit
// and io.of(nsp).to(id).emit for broadcasts.
const io: any = { of: () => ({ emit: () => {}, to: () => ({ emit: () => {} }) }) };

function fakeSocket(id: string, ip: string) {
  const handlers: Record<string, (...a: any[]) => void> = {};
  return {
    id,
    nsp: { name: `/sol/${ip}` },
    handshake: { address: "9.9.9.9" },
    on: (event: string, cb: (...a: any[]) => void) => { handlers[event] = cb; },
    emit: () => {},
    handlers,
  } as any;
}

describe("applyLockHandlers — live LockManager lookup", () => {
  beforeEach(() => { sessions.clear(); });

  it("acts on the LIVE LockManager after a power cycle, not the one captured at connect", () => {
    const ip = "10.1.0.1";
    startSession(ip);                          // live — LockManager A
    const sock = fakeSocket("clientX", ip);
    applyLockHandlers(io, sock, ip);           // wired once, while A is current
    stopSession(ip);                           // power-off — replaces lockManager (B)
    startSession(ip);                          // power-on  — replaces lockManager again (C, fresh)

    sock.handlers["lock:request"]();           // client (still connected) requests the lock

    // The lock must land on the CURRENT session.lockManager (C), which is what
    // terminal:input's gate reads — not the orphaned A captured at connect.
    expect(getSession(ip)!.lockManager.isHeldBy("clientX")).toBe(true);
  });

  it("gives a client that connected while pending a working lock once its blade powers on", () => {
    const ip = "10.1.0.2";                     // no session yet (blade off)
    const sock = fakeSocket("clientY", ip);
    applyLockHandlers(io, sock, ip);           // wired while pending

    expect(getSession(ip)).toBeUndefined();
    expect(() => sock.handlers["lock:request"]()).not.toThrow();  // no-op while pending

    startSession(ip);                          // blade powers on
    sock.handlers["lock:request"]();           // same socket, no reconnect

    expect(getSession(ip)!.lockManager.isHeldBy("clientY")).toBe(true);
  });

  it("releases the live lock on disconnect", () => {
    const ip = "10.1.0.3";
    startSession(ip);
    const sock = fakeSocket("clientZ", ip);
    applyLockHandlers(io, sock, ip);
    sock.handlers["lock:request"]();
    expect(getSession(ip)!.lockManager.isHeldBy("clientZ")).toBe(true);

    sock.handlers["disconnect"]();
    expect(getSession(ip)!.lockManager.isHeld()).toBe(false);
  });
});
