import express, { NextFunction, Request, Response } from 'express';
import http from 'http';
import { Server } from 'socket.io';
import { registerTerminalNamespace } from './io/handlers/terminal';
import { startReconcileLoop } from './reconcile';
import { hasLiveSession, replaceSession, setIo, startSession } from './sessions/manager';
import { logger } from './utils/logger';

export const app = express();
app.use(express.json());

// CORS for the post web UI, which is served cross-origin (the static UI is on
// a different origin than this server). socket.io has its own cors config;
// REST routes below need their own headers + OPTIONS preflight handling.
app.use((req: Request, res: Response, next: NextFunction) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') {
    res.sendStatus(204);
    return;
  }
  next();
});

// GET /healthz — liveness probe for the deploy unit / load balancer.
app.get('/healthz', (_req: Request, res: Response) => {
  res.json({ status: 'ok' });
});

const app_server = http.createServer(app);
const io = new Server(app_server, {
  cors: { origin: '*' }
});

registerTerminalNamespace(io);
setIo(io);

// Session lifecycle is reconcile-driven: poll the post viewer's blade
// inventory and start SOL sessions for reachable BMCs (bmc_ip set AND
// bmc_pinged), replacing a slot's session only on a device swap (a new
// bmc_mac at the port-deterministic slot ip). A failed poll pauses
// reconciliation (existing sessions and their retained buffers are left
// alone) rather than tearing anything down; power-off/unreachable/vanished
// blades are keep-on-loss — never auto-torn-down.
const BLADES_URL = process.env.POST_BLADES_URL ?? 'http://127.0.0.1:8446/api/v1/blades';
const RECONCILE_MS = Number(process.env.POST_SOL_RECONCILE_SECS ?? 15) * 1000;
startReconcileLoop(
  { bladesUrl: BLADES_URL, hasSession: hasLiveSession, start: startSession, replace: replaceSession },
  RECONCILE_MS
);

const PORT = process.env.PORT || 5559;
// Bind loopback only. This relay grants un-TLS'd root serial access; under
// `--network host` a bare listen(PORT) would expose it on every host
// interface. nginx proxies from 127.0.0.1 and POST_BLADES_URL is loopback,
// so binding 127.0.0.1 hardens without breaking anything.
app_server.listen(Number(PORT), "127.0.0.1", () => {
  logger.info(`Server listening on http://127.0.0.1:${PORT}`);
});
