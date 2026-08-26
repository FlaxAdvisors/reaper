import { describe, it, expect } from "vitest";
import request from "supertest";
import { app } from "./index";

describe("healthz", () => {
  it("returns ok", async () => {
    const res = await request(app).get("/healthz");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: "ok" });
  });
});
