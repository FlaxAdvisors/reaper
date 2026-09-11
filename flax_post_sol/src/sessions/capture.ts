/* src/sessions/capture.ts
 *
 * Per-slot console capture on disk (spec 2026-09-11 post-slot-ladder §8).
 * Every pty chunk is appended to <dir>/<ip>.txt; POST /mark/:ip rotates it to
 * <ip>.prev.txt and starts a fresh file with one header line, so the file the
 * engine stores at verdict time covers exactly this boot. Errors are logged,
 * never thrown: a full disk must not kill the SOL relay.
 */
import fs from "fs";
import path from "path";
import { logger } from "../utils/logger";

export const CAPTURE_DIR = process.env.POST_SOL_CAPTURE_DIR ?? "/var/lib/flax/post-sol";

export function capturePath(ip: string, dir: string = CAPTURE_DIR): string {
  return path.join(dir, `${ip}.txt`);
}

export function appendCapture(ip: string, data: string, dir: string = CAPTURE_DIR): void {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(capturePath(ip, dir), data);
  } catch (error) {
    logger.error(`[${ip}] capture append failed: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export function markCapture(ip: string, reason: string, now: Date = new Date(), dir: string = CAPTURE_DIR): string {
  const header = `=== flax-post mark ${now.toISOString()} ${reason} ===\n`;
  try {
    fs.mkdirSync(dir, { recursive: true });
    const cur = capturePath(ip, dir);
    if (fs.existsSync(cur)) fs.renameSync(cur, path.join(dir, `${ip}.prev.txt`));
    fs.writeFileSync(cur, header);
  } catch (error) {
    logger.error(`[${ip}] capture mark failed: ${error instanceof Error ? error.message : String(error)}`);
  }
  return header;
}
