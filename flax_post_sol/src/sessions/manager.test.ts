import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// spawn() returns a controllable pty: onExit stores the callback on the object
// (as _exit) so a test can simulate an unexpected pty death (crash) by invoking
// it. onData is a no-op (we don't drive terminal data in these tests).
vi.mock("node-pty", () => ({
  spawn: () => {
    const obj: any = { onData: vi.fn(), write: vi.fn(), kill: vi.fn() };
    obj.onExit = (cb: (e: { exitCode?: number; signal?: number | null }) => void) => {
      obj._exit = cb;
    };
    return obj;
  },
}));

import {
  startSession,
  stopSession,
  getSession,
  runningIps,
  sessions,
  setIo,
  relaunchSession,
  replaceSession,
  hasLiveSession,
} from "./manager";

describe("session retention", () => {
  beforeEach(() => {
    sessions.clear();
  });

  it("keeps the buffer on stop and clears it only on re-start", () => {
    const s = startSession("10.0.0.9");
    s.historyBuffer.push("BOOT LOG\r\n");
    stopSession("10.0.0.9");
    const ended = getSession("10.0.0.9")!;
    expect(ended.state).toBe("ended");
    expect(ended.historyBuffer.join("")).toContain("BOOT LOG");   // retained after power-off
    expect(runningIps().has("10.0.0.9")).toBe(false);             // not "running" for reconcile

    startSession("10.0.0.9");                                     // powered back on
    expect(getSession("10.0.0.9")!.state).toBe("live");
    expect(getSession("10.0.0.9")!.historyBuffer.join("")).toBe(""); // buffer reset on re-start
  });

  it("stopSession on an unknown ip is a no-op (no throw)", () => {
    expect(() => stopSession("10.0.0.99")).not.toThrow();
    expect(getSession("10.0.0.99")).toBeUndefined();
  });

  it("stopSession kills the pty and nulls it out (frees the held IPMI session)", () => {
    const s = startSession("10.0.0.5");
    const killSpy = s.ptyProcess!.kill as ReturnType<typeof vi.fn>;
    stopSession("10.0.0.5");
    expect(killSpy).toHaveBeenCalled();
    expect(getSession("10.0.0.5")!.ptyProcess).toBeNull();
  });

  it("runningIps only reflects live sessions across multiple ips", () => {
    startSession("10.0.0.1");
    startSession("10.0.0.2");
    stopSession("10.0.0.2");
    expect([...runningIps()].sort()).toEqual(["10.0.0.1"]);
  });

  it("startSession releases any stale lock held from a prior boot", () => {
    const s = startSession("10.0.0.7");
    s.lockManager.acquire("client-a");
    expect(s.lockManager.isHeld()).toBe(true);
    stopSession("10.0.0.7");
    const restarted = startSession("10.0.0.7");
    expect(restarted.lockManager.isHeld()).toBe(false);
  });
});

describe("relaunch / replace (reachability + device-replacement reconcile)", () => {
  beforeEach(() => {
    sessions.clear();
  });

  it("hasLiveSession reflects only live sessions", () => {
    expect(hasLiveSession("10.9.9.1")).toBe(false);
    startSession("10.9.9.1");
    expect(hasLiveSession("10.9.9.1")).toBe(true);
    stopSession("10.9.9.1");
    expect(hasLiveSession("10.9.9.1")).toBe(false);
  });

  it("relaunchSession kills the old pty, resets buffer + lock, respawns", () => {
    const s = startSession("10.9.9.9"); s.historyBuffer.push("OLD"); s.lockManager.acquire("c1");
    relaunchSession("10.9.9.9");
    const n = getSession("10.9.9.9")!;
    expect(n.state).toBe("live"); expect(n.historyBuffer.join("")).toBe(""); expect(n.lockManager.isHeld()).toBe(false);
  });
  it("replaceSession with a different IP stops the old (retains its buffer) and starts the new", () => {
    const o = startSession("10.9.0.1"); o.historyBuffer.push("KEEP");
    replaceSession("10.9.0.1", "10.9.0.2");
    expect(getSession("10.9.0.1")!.state).toBe("ended");
    expect(getSession("10.9.0.1")!.historyBuffer.join("")).toContain("KEEP");   // old retained
    expect(getSession("10.9.0.2")!.state).toBe("live");
  });
  it("replaceSession with the SAME IP relaunches in place (buffer reset, no teardown)", () => {
    // The common production case: a new device at the same port-deterministic
    // slot IP. No stopSession — just relaunch-in-place with a fresh buffer.
    const o = startSession("10.9.0.3"); o.historyBuffer.push("OLDDEV");
    replaceSession("10.9.0.3", "10.9.0.3");
    const n = getSession("10.9.0.3")!;
    expect(n.state).toBe("live");
    expect(n.historyBuffer.join("")).toBe("");   // reset for the new device
  });
});

describe("crash recovery — unexpected pty exit flips session to ended", () => {
  // attachPtyEvents only runs when an io is set, so give these tests a minimal
  // fake io. The onExit handler doesn't use io (it only logs + flips state).
  function fakeIo() {
    const emit = () => {};
    return { of: () => ({ emit, to: () => ({ emit }) }) } as any;
  }
  beforeEach(() => {
    sessions.clear();
    setIo(fakeIo());
  });
  afterEach(() => {
    setIo(undefined as any); // don't leak the fake io into other test files
  });

  it("an unexpected pty exit marks the session ended and nulls the pty (so reconcile can recover)", () => {
    const s = startSession("10.7.7.1");
    const pty = s.ptyProcess as any;
    expect(hasLiveSession("10.7.7.1")).toBe(true);
    pty._exit({ exitCode: 1, signal: 9 });        // soltriage/ipmitool/bash died
    expect(getSession("10.7.7.1")!.state).toBe("ended");
    expect(getSession("10.7.7.1")!.ptyProcess).toBeNull();
    expect(hasLiveSession("10.7.7.1")).toBe(false);
  });

  it("a stale onExit from a superseded pty does NOT clobber the fresh session (relaunch race)", () => {
    const s = startSession("10.7.7.2");
    const oldPty = s.ptyProcess as any;
    relaunchSession("10.7.7.2");                    // spawns a new pty for the same ip
    const newPty = getSession("10.7.7.2")!.ptyProcess as any;
    expect(newPty).not.toBe(oldPty);
    oldPty._exit({ exitCode: 0, signal: null });    // late exit of the killed old pty
    expect(getSession("10.7.7.2")!.state).toBe("live");     // fresh session untouched
    expect(getSession("10.7.7.2")!.ptyProcess).toBe(newPty);
  });
});

describe("lock:status broadcast on power-cycle", () => {
  // Record every (event, payload) emitted to any namespace so we can assert
  // the lock UI is resynced whenever the LockManager is swapped.
  function fakeIoRecording() {
    const emitted: Array<{ event: string; payload: unknown }> = [];
    const emit = (event: string, payload: unknown) => { emitted.push({ event, payload }); };
    const io: any = { of: () => ({ emit, to: () => ({ emit }) }) };
    return { io, emitted };
  }

  beforeEach(() => {
    sessions.clear();
  });

  it("emits lock:status {holder:null,isHeld:false} on both stop and (re)start", () => {
    const { io, emitted } = fakeIoRecording();
    setIo(io);
    try {
      startSession("10.0.0.30");
      stopSession("10.0.0.30");
      startSession("10.0.0.30");

      const lockStatuses = emitted.filter((e) => e.event === "lock:status");
      // one on each of start, stop, start
      expect(lockStatuses.length).toBe(3);
      for (const s of lockStatuses) {
        expect(s.payload).toEqual({ holder: null, isHeld: false });
      }
    } finally {
      setIo(undefined as any); // don't leak the fake io into other test files
    }
  });
});
