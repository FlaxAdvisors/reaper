/* back/src/sessions/types.ts */
import { IPty } from "node-pty";
import { LockManager } from "./lock";


export interface LockStatus {
    holder: string | null;
    requestedBy: Set<string>;
}

export interface Session {
    ip: string;
    ptyProcess: IPty | null;
    clients: Set<string>;
    lock: LockStatus;
    lockManager: LockManager;
    historyBuffer: string[];
    /**
     * "live"    — pty is spawned and running soltriage against a powered-on blade.
     * "ended"   — blade was powered off (or never started); historyBuffer is
     *             RETAINED for viewing and only cleared by the next startSession
     *             (power-on). There is no TTL/timer-based eviction.
     */
    state: "live" | "ended";
    /** epoch ms of the last pty chunk (idle indicator; drop detection). */
    lastDataAt?: number;
    /** epoch ms of the last automatic relaunch (cooldown for drop detection). */
    lastAutoRelaunch?: number;
    /** rolling tail of the stream the drop-message matcher looks at. */
    tailText?: string;
}
