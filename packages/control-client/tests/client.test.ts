import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { connect, RpcError, type EmuEvent } from "../src/index.ts";

type Handler = (method: string, params: any, reply: (msg: object) => void) => void;

async function fakeServer(handler: Handler) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "digi-ts-"));
  const sock = path.join(dir, "t.sock");
  const server = net.createServer((conn) => {
    let buf = "";
    conn.setEncoding("utf8");
    conn.on("data", (chunk: string) => {
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const req = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        const reply = (msg: object) => conn.write(`${JSON.stringify(msg)}\n`);
        if (req.method === "hello") reply({ jsonrpc: "2.0", id: req.id, result: { schema_version: 1 } });
        else handler(req.method, req.params, (m) => reply({ jsonrpc: "2.0", id: req.id, ...m }));
      }
    });
  });
  await new Promise<void>((r) => server.listen(sock, r));
  return {
    sock,
    close: () => {
      server.close();
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

test("call returns the result and maps errors", async () => {
  const srv = await fakeServer((method, params, reply) => {
    if (method === "step") reply({ result: { icount: params.instructions } });
    else reply({ error: { code: 6, message: "guest fault", data: { pc: 1 } } });
  });
  const c = await connect(srv.sock);
  assert.deepEqual(await c.step(5), { icount: 5 });
  await assert.rejects(c.status(), (e: unknown) => {
    assert.ok(e instanceof RpcError);
    assert.equal(e.code, 6);
    assert.deepEqual(e.data, { pc: 1 });
    return true;
  });
  c.close();
  srv.close();
});

test("frameHash asks for hash without data", async () => {
  let seen: unknown;
  const srv = await fakeServer((_m, params, reply) => {
    seen = params;
    reply({ result: { revision: 1, sha256: "ab" } });
  });
  const c = await connect(srv.sock);
  assert.equal(await c.frameHash(), "ab");
  assert.deepEqual(seen, { hash: true, data: false });
  c.close();
  srv.close();
});

test("event notifications are buffered, bounded, and iterable", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "digi-ts-"));
  const sock = path.join(dir, "e.sock");
  const server = net.createServer((conn) => {
    conn.setEncoding("utf8");
    conn.on("data", (chunk: string) => {
      for (const line of chunk.split("\n").filter(Boolean)) {
        const req = JSON.parse(line);
        const result = req.method === "hello" ? { schema_version: 1 } : {};
        if (req.method === "events.subscribe") {
          for (let i = 0; i < 5; i++) {
            conn.write(`${JSON.stringify({ jsonrpc: "2.0", method: "event", params: { kind: "mode", i } })}\n`);
          }
          conn.write(`${JSON.stringify({ jsonrpc: "2.0", method: "event", params: { kind: "lagged", n: 2 } })}\n`);
        }
        conn.write(`${JSON.stringify({ jsonrpc: "2.0", id: req.id, result })}\n`);
      }
    });
  });
  await new Promise<void>((r) => server.listen(sock, r));
  const c = await connect(sock, { maxBufferedEvents: 3 });
  await c.eventsSubscribe();
  const got: EmuEvent[] = [];
  const abort = new AbortController();
  for await (const ev of c.events(abort.signal)) {
    got.push(ev);
    if (got.length === 3) abort.abort();
  }
  assert.equal(c.dropped, 3);
  assert.deepEqual(got.map((e) => e.kind), ["mode", "mode", "lagged"]);
  c.close();
  server.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

test("pending calls reject when the server closes", async () => {
  const srv = await fakeServer(() => {});
  const c = await connect(srv.sock);
  const p = c.status();
  srv.close();
  c.close();
  await assert.rejects(p);
});
