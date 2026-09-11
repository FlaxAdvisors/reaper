import { describe, it, expect } from "vitest";
import fs from "fs";
import os from "os";
import path from "path";
import { appendCapture, capturePath, markCapture } from "./capture";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "post-sol-"));

describe("capture", () => {
  it("appends chunks to <dir>/<ip>.txt, creating the dir", () => {
    const dir = path.join(tmp(), "nested");
    appendCapture("10.0.0.1", "POST 0x01\r\n", dir);
    appendCapture("10.0.0.1", "POST 0x02\r\n", dir);
    expect(fs.readFileSync(capturePath("10.0.0.1", dir), "utf8")).toBe("POST 0x01\r\nPOST 0x02\r\n");
  });
  it("mark rotates the current file to .prev and writes a header", () => {
    const dir = tmp();
    appendCapture("10.0.0.1", "old boot\n", dir);
    const header = markCapture("10.0.0.1", "power-on", new Date("2026-09-11T14:00:00Z"), dir);
    expect(header).toBe("=== flax-post mark 2026-09-11T14:00:00.000Z power-on ===\n");
    expect(fs.readFileSync(capturePath("10.0.0.1", dir), "utf8")).toBe(header);
    expect(fs.readFileSync(path.join(dir, "10.0.0.1.prev.txt"), "utf8")).toBe("old boot\n");
  });
  it("append rotates to .prev with a header once the file passes the size cap", () => {
    const dir = tmp();
    appendCapture("10.0.0.3", "0123456789", dir, 4);       // 10 bytes, under a fresh file
    expect(fs.existsSync(path.join(dir, "10.0.0.3.prev.txt"))).toBe(false);
    appendCapture("10.0.0.3", "NEW BOOT\n", dir, 4);       // now over the cap -> rotate
    const cur = fs.readFileSync(capturePath("10.0.0.3", dir), "utf8");
    expect(cur).toMatch(/^=== flax-post capture rotated \S+ size-cap ===\n/);
    expect(cur).toContain("NEW BOOT\n");
    expect(fs.readFileSync(path.join(dir, "10.0.0.3.prev.txt"), "utf8")).toBe("0123456789");
  });
  it("append rotation replaces an older .prev instead of failing", () => {
    const dir = tmp();
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "10.0.0.4.prev.txt"), "ancient\n");
    appendCapture("10.0.0.4", "0123456789", dir, 4);
    appendCapture("10.0.0.4", "x", dir, 4);
    expect(fs.readFileSync(path.join(dir, "10.0.0.4.prev.txt"), "utf8")).toBe("0123456789");
  });
  it("append with the cap disabled never rotates", () => {
    const dir = tmp();
    appendCapture("10.0.0.5", "0123456789", dir, 0);
    appendCapture("10.0.0.5", "abc", dir, 0);
    expect(fs.readFileSync(capturePath("10.0.0.5", dir), "utf8")).toBe("0123456789abc");
    expect(fs.existsSync(path.join(dir, "10.0.0.5.prev.txt"))).toBe(false);
  });
  it("mark on a slot with no file just writes the header", () => {
    const dir = tmp();
    markCapture("10.0.0.2", "operator", new Date("2026-09-11T14:00:00Z"), dir);
    expect(fs.existsSync(path.join(dir, "10.0.0.2.prev.txt"))).toBe(false);
    expect(fs.readFileSync(capturePath("10.0.0.2", dir), "utf8")).toContain("operator");
  });
});
