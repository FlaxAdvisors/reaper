/* back/src/io/handlers/lock.ts */
import { Server, Socket } from "socket.io";
import { getSession } from "../../sessions/manager";
import { logger } from "../../utils/logger";

/**
 * Write-lock socket handlers. These are wired for EVERY connecting client —
 * live, ended, or still-pending (blade off, no session yet). Each callback
 * looks the LockManager up LIVE via getSession(ip) rather than capturing an
 * instance at connect time, because startSession/stopSession replace
 * session.lockManager on every power cycle. A client that stays connected
 * across a reboot therefore always acts on the current lock, and a client
 * that connected while pending starts working the moment the session exists —
 * no reconnect required.
 */
export function applyLockHandlers(io: Server, socket: Socket, ip: string) {
    const clientId = socket.id;
    const nspName = socket.nsp.name;
    const lock = () => getSession(ip)?.lockManager ?? null;
    const broadcast = () => {
        const m = lock();
        io.of(nspName).emit("lock:status", {
            holder: m?.status() ?? null,
            isHeld: m?.isHeld() ?? false,
        });
    };

    socket.on("lock:request", () => {
        const m = lock();
        if (!m) {
            logger.info(`[${ip}] lock:request from ${clientId} ignored — no live session`);
            return;
        }
        if (m.acquire(clientId)) {
            logger.info(`[${ip}] Lock acquired by ${clientId}`);
        } else {
            logger.info(`[${ip}] Lock request denied for ${clientId} (held by ${m.status()})`);
        }
        broadcast();
    });

    socket.on("lock:release", () => {
        const m = lock();
        if (!m) return;
        if (m.release(clientId)) {
            logger.info(`[${ip}] Lock released by ${clientId}`);
        } else {
            logger.info(`[${ip}] Lock release denied for ${clientId} (not holder)`);
        }
        broadcast();
    });

    socket.on("lock:request_release", () => {
        const currentHolder = lock()?.status() ?? null;
        if (currentHolder) {
            logger.info(`[${ip}] Client ${clientId} requesting release from holder ${currentHolder}`);
            io.of(nspName).to(currentHolder).emit("lock:release_requested", {
                requesterId: clientId,
                requesterIp: socket.handshake.address, // Optional: provide requester info
            });
        } else {
            logger.info(`[${ip}] Client ${clientId} requested release, but no lock is held.`);
            socket.emit("lock:request_release_status", { message: "No lock is currently held." });
        }
    });

    socket.on("disconnect", () => {
        const m = lock();
        if (m && m.isHeldBy(clientId)) {
            m.release(clientId);
            logger.info(`[${ip}] Lock released due to disconnect: ${clientId}`);
            broadcast();
        }
    });
}
