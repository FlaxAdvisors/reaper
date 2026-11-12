// src/io/handlers/terminal.ts
import { Server, Socket } from "socket.io";
import { addPendingClient, getSession, relaunchSession, removePendingClient } from "../../sessions/manager";
import { logger } from "../../utils/logger";
import { applyLockHandlers } from "./lock";

/**
 * Client connects are attach-only: a session is created/destroyed ONLY by
 * the reconcile loop (see ../../reconcile.ts), driven off which blades are
 * powered on. Connecting here never spawns a pty.
 *
 *  - Live session exists  -> attach, replay historyBuffer, emit sol:status live.
 *  - Ended session exists -> attach (so retained history is viewable), replay
 *                            historyBuffer, emit sol:status ended.
 *  - No session yet       -> hold the socket as "pending" for this ip and emit
 *                            sol:status off; startSession(ip) will move it
 *                            into session.clients once the blade powers on.
 */
export function registerTerminalNamespace(io: Server) {
    io.of(/^\/sol\/[\d.]+$/).on("connection", (socket: Socket) => {
        const namespace = socket.nsp;
        const ip = namespace.name.split("/").pop()!;
        const clientId = socket.id;

        logger.info(`[${ip}] Client connected: ${clientId}`);

        const session = getSession(ip);

        if (!session) {
            addPendingClient(ip, clientId);
            socket.emit("sol:status", { state: "off" });
            logger.info(`[${ip}] No session yet for ${clientId}; held as pending`);
        } else {
            session.clients.add(clientId);

            const historyContent = session.historyBuffer.join("");
            if (historyContent.length > 0) {
                // Replay uses terminal:history (distinct from live terminal:data) so the
                // client can both render it AND log a "history received" event.
                socket.emit("terminal:history", historyContent);
                logger.info(`[${ip}] Sent history (${session.historyBuffer.length} chunks) to new client: ${clientId}`);
            }

            socket.emit("sol:status", { state: session.state });

            io.of(namespace.name).emit("client_count_update", session.clients.size);

            socket.emit("lock:status", {
                holder: session.lockManager.status(),
                isHeld: session.lockManager.isHeld(),
            });
            logger.info(`[${ip}] Sent initial lock status to new client: ${clientId}`);
        }

        socket.on("terminal:input", (data: string) => {
            try {
                const current = getSession(ip);
                if (current && current.ptyProcess && current.state === "live") {
                    // Check if THIS client holds the lock before writing to PTY
                    if (current.lockManager.isHeldBy(clientId)) {
                        logger.debug(`[${ip}] Input accepted from lock holder ${clientId}: ${data.substring(0, 20)}...`);
                        current.ptyProcess.write(data);
                    } else {
                        logger.warn(`[${ip}] Input denied for ${clientId} - Lock not held (held by ${current.lockManager.status() || "none"})`);
                    }
                } else {
                    logger.warn(`[${ip}] Received input for non-live session from ${clientId}`);
                }
            } catch (error) {
                if (error instanceof Error) {
                    logger.error(`[${ip}] Error writing to PTY for ${clientId}: ${error.message}`);
                } else {
                    logger.error(`[${ip}] Unknown error writing to PTY for ${clientId}: ${String(error)}`);
                }
            }
        });

        socket.on("sol:relaunch", () => {
            logger.info(`[${ip}] relaunch requested by ${clientId}`);
            relaunchSession(ip);
        });

        socket.on("disconnect", () => {
            logger.info(`[${ip}] Client disconnected: ${clientId}`);
            removePendingClient(ip, clientId);
            const current = getSession(ip);
            if (current) {
                current.clients.delete(clientId);
                io.of(namespace.name).emit("client_count_update", current.clients.size);
            }
            // Session lifecycle belongs to the reconcile loop, not client
            // presence — a session is never stopped/removed here.
        });

        // Wire lock handlers for EVERY client — live, ended, or pending. They
        // resolve the LockManager live via getSession(ip) inside each callback,
        // so they survive the LockManager being replaced across a power cycle
        // and start working the moment a pending client's blade powers on. No
        // reconnect required.
        applyLockHandlers(io, socket, ip);
    });
}
