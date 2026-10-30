import { describe, it, expect, vi } from "vitest";
import { fetchAllReach, fetchBladeReach, makeReconciler } from "./reconcile";

const blade = (o: any) => ({ port:"et5b1", bmc_mac:"aa", bmc_ip:"10.0.0.1", bmc_pinged:true, ...o });

describe("fetchBladeReach", () => {
  it("keeps reserved slots, marks reachable = bmc_pinged, drops empty/no-mac/no-ip and non-ip", async () => {
    const f = (async () => ({ ok:true, json: async () => ({ slots:[
      blade({}),                                              // reachable
      blade({port:"et5b2", bmc_ip:"10.0.0.2", bmc_pinged:false}), // reserved not-pingable
      blade({port:"et5b3", bmc_mac:null}),                   // no mac -> drop
      blade({port:"et5b4", bmc_ip:null}),                    // no ip -> drop
      blade({port:"et5b5", bmc_ip:"1.2.3.4; rm -rf /"}),     // non-ip -> drop (shell guard)
      {port:"et5b6", empty:true},
    ] }) })) as any;
    const r = await fetchBladeReach("x", f);
    expect(r.map(b=>[b.port,b.reachable])).toEqual([["et5b1",true],["et5b2",false]]);
  });
});

describe("fetchAllReach", () => {
  const byUrl = (feeds: Record<string, any[] | null>) => (async (u: string) => {
    const slots = feeds[u];
    if (slots === null) throw new Error("connection refused");
    return { ok: true, json: async () => ({ slots }) };
  }) as any;

  it("unions the post blades and the rackpost targets", async () => {
    const f = byUrl({ post: [blade({})],
                      rack: [blade({ port: "rackpost:rr101:L37", bmc_mac: "cc", bmc_ip: "172.32.37.11" })] });
    const r = await fetchAllReach(["post", "rack"], f);
    expect(r.map(b => [b.port, b.ip])).toEqual([["et5b1", "10.0.0.1"], ["rackpost:rr101:L37", "172.32.37.11"]]);
  });
  it("keeps the feed that answered when the other is down", async () => {
    const r = await fetchAllReach(["post", "rack"], byUrl({ post: [blade({})], rack: null }));
    expect(r.map(b => b.port)).toEqual(["et5b1"]);
  });
  it("throws only when every feed fails", async () => {
    await expect(fetchAllReach(["post", "rack"], byUrl({ post: null, rack: null }))).rejects.toThrow();
  });
});

describe("makeReconciler", () => {
  const deps = () => { const calls:any[]=[]; const live=new Set<string>(); return { calls, live,
    d: { bladesUrls:["x"],
         hasSession:(ip:string)=>live.has(ip),
         start:(ip:string)=>{ calls.push(["start",ip]); live.add(ip); },
         replace:(o:string,n:string)=>{ calls.push(["replace",o,n]); live.delete(o); live.add(n); },
         fetchFn: null as any } }; };
  const feed = (d:any, slots:any[]) => { d.fetchFn = (async()=>({ok:true,json:async()=>({slots})}))as any; };

  it("starts a reachable new port; skips an unreachable one", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({}), blade({port:"et5b2",bmc_mac:"bb",bmc_ip:"10.0.0.2",bmc_pinged:false})]);
    await r.tick();
    expect(calls).toEqual([["start","10.0.0.1"]]);
  });
  it("keeps on loss: a started port going unreachable/vanished triggers no stop/replace", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({})]); await r.tick();                     // start
    feed(d,[blade({bmc_pinged:false})]); await r.tick();     // unreachable -> no-op
    feed(d,[]); await r.tick();                              // vanished -> no-op
    expect(calls).toEqual([["start","10.0.0.1"]]);
  });
  it("replaces on a new reachable mac at the port (same slot IP)", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({})]); await r.tick();
    feed(d,[blade({bmc_mac:"bb"})]); await r.tick();         // new device, same ip
    expect(calls).toEqual([["start","10.0.0.1"],["replace","10.0.0.1","10.0.0.1"]]);
  });
  it("recovers a vanished session: same mac + reachable but no live session -> starts again", async () => {
    const {calls,live,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({})]); await r.tick();                     // start
    live.delete("10.0.0.1");                                 // session crashed/vanished
    feed(d,[blade({})]); await r.tick();                     // same mac, reachable, no session -> recover
    expect(calls).toEqual([["start","10.0.0.1"],["start","10.0.0.1"]]);
  });
  it("does not replace for a new mac that is not yet pingable", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({})]); await r.tick();
    feed(d,[blade({bmc_mac:"bb",bmc_pinged:false})]); await r.tick();  // new mac, unreachable
    expect(calls).toEqual([["start","10.0.0.1"]]);
  });
  it("pauses (no start/replace) when the blades fetch fails", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    feed(d,[blade({})]); await r.tick();
    d.fetchFn = (async()=>{throw new Error("down");}) as any; await r.tick();
    expect(calls).toEqual([["start","10.0.0.1"]]);
  });
  it("starts rackpost targets beside post blades; rackpost down pauses only its own slots", async () => {
    const {calls,d}=deps(); const r=makeReconciler(d);
    d.bladesUrls = ["post", "rack"];
    const rack = blade({ port: "rackpost:rr101:L37", bmc_mac: "cc", bmc_ip: "172.32.37.11" });
    d.fetchFn = (async (u: string) => ({ ok: true, json: async () => ({ slots: u === "post" ? [blade({})] : [rack] }) })) as any;
    await r.tick();
    d.fetchFn = (async (u: string) => { if (u === "rack") throw new Error("down");
      return { ok: true, json: async () => ({ slots: [blade({}), blade({ port: "et5b2", bmc_mac: "dd", bmc_ip: "10.0.0.2" })] }) }; }) as any;
    await r.tick();
    expect(calls).toEqual([["start","10.0.0.1"],["start","172.32.37.11"],["start","10.0.0.2"]]);
  });
});
