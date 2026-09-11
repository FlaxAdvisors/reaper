/* src/sessions/capture.ts
 *
 * Per-slot console capture on disk (spec 2026-09-11 post-slot-ladder §8).
 * Every pty chunk is appended to <dir>/<ip>.txt; POST /mark/:ip rotates it to
 * <ip>.prev.txt and starts a fresh file with one header line, so the file the
 * engine stores at verdict time covers exactly this boot. Errors are logged,
 * never thrown: a full disk must not kill the SOL relay.
 *
 * The only bound on a capture is /mark, which is a per-BOOT event: a blade
 * that sits at a console spewing (a kernel oops loop, a chatty BIOS) between
 * marks writes without limit, and nothing here is under logrotate. So append
 * enforces a size cap of its own -- the same rotate-to-.prev as /mark, with a
 * header saying why -- and the store stays bounded at 2 * cap per slot.
 */
import fs from "fs";
import path from "path";
import { logger } from "../utils/logger";

export const CAPTURE_DIR = process.env.POST_SOL_CAPTURE_DIR ?? "/var/lib/flax/post-sol";
export const CAPTURE_MAX_BYTES = Number(process.env.POST_SOL_CAPTURE_MAX_BYTES ?? 8 * 1024 * 1024);

export function capturePath(ip: string, dir: string = CAPTURE_DIR): string {
  return path.join(dir, `${ip}.txt`);
}

function currentSize(file: string): number {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

export function appendCapture(
  ip: string,
  data: string,
  dir: string = CAPTURE_DIR,
  maxBytes: number = CAPTURE_MAX_BYTES,
): void {
  try {
    fs.mkdirSync(dir, { recursive: true });
    const cur = capturePath(ip, dir);
    if (maxBytes > 0 && currentSize(cur) > maxBytes) {
      fs.renameSync(cur, path.join(dir, `${ip}.prev.txt`));
      fs.writeFileSync(cur, `=== flax-post capture rotated ${new Date().toISOString()} size-cap ===\n`);
      logger.info(`[${ip}] capture rotated at ${maxBytes} bytes`);
    }
    fs.appendFileSync(cur, data);
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
