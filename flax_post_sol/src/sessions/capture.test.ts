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
  it("mark on a slot with no file just writes the header", () => {
    const dir = tmp();
    markCapture("10.0.0.2", "operator", new Date("2026-09-11T14:00:00Z"), dir);
    expect(fs.existsSync(path.join(dir, "10.0.0.2.prev.txt"))).toBe(false);
    expect(fs.readFileSync(capturePath("10.0.0.2", dir), "utf8")).toContain("operator");
  });
});
