#!/usr/bin/env node
// A live check of the TypeScript Transfer client (apps/web/src/modules/dkupload-core.ts, dktransfer.ts) against a running emulator, through
// its control socket (the same `midi.usb.in` / `midi.usb.out` the desktop page reaches with `emu_call`). It is a dev tool: the app itself
// starts no process. Start the target first (`mise run emu:serve -- FIRMWARE --free`, or a daemon with `run.mode free`), then:
//
//     node tools/devtools/transfer_live.mjs SOCKET [--restart] [--delete]
//
// It reads the library folder out/modules/<device>/ (mise run module:pack-all), lists, installs the first Machines module that is on no
// card yet, lists again, SELECTs it (the page never does; the check does to see a module load), optionally RESTARTs and checks MODULES,
// and optionally deletes the file. Exit code 0 when every step did what it should.
import net from 'node:net';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { DkSession } from '../../apps/web/src/modules/dkupload-core.ts';
import { splitMessages } from '../../apps/web/src/modules/dkupload-core.ts';
import {
  buildView,
  deleteFiles,
  install,
  libraryModule,
  restartDevice,
} from '../../apps/web/src/modules/dktransfer.ts';

const [socketPath, ...flags] = process.argv.slice(2);
if (!socketPath) {
  console.error('usage: node tools/devtools/transfer_live.mjs SOCKET [--restart] [--delete]');
  process.exit(2);
}

// ---- JSON-RPC over the control socket ----
const sock = net.connect(socketPath);
let buf = '';
let nextId = 1;
const waiting = new Map();
sock.on('data', (d) => {
  buf += d;
  for (let i = buf.indexOf('\n'); i >= 0; i = buf.indexOf('\n')) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    const m = JSON.parse(line);
    if (m.id !== undefined && waiting.has(m.id)) {
      const { ok, fail } = waiting.get(m.id);
      waiting.delete(m.id);
      if (m.error) fail(new Error(m.error.message));
      else ok(m.result);
    }
  }
});
const call = (method, params = {}) =>
  new Promise((ok, fail) => {
    const id = nextId++;
    waiting.set(id, { ok, fail });
    sock.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
await call('hello', { min_schema: 1 });

const hex = (b) => Buffer.from(b).toString('hex');
class Transport {
  rx = new Uint8Array();
  ready = [];
  async send(msg) {
    await call('midi.usb.in', { bytes: hex(msg), chunk: 384, period: 16500 });
  }
  async recv(timeoutMs) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const next = this.ready.shift();
      if (next) return next;
      const { hex: got } = await call('midi.usb.out');
      if (got) {
        const { messages, rest } = splitMessages(Uint8Array.from(Buffer.concat([this.rx, Buffer.from(got, 'hex')])));
        this.rx = rest;
        this.ready.push(...messages);
        continue;
      }
      if (Date.now() >= end) return null;
      await new Promise((r) => setTimeout(r, 10));
    }
  }
}

const checks = [];
const check = (name, ok, detail = '') => {
  checks.push(ok);
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name} ${detail}`);
};
const s = new DkSession(new Transport(), { timeoutMs: 6000 });
const info = await s.info();
console.log(`device ${info.port}, runtime ${info.runtime.toString(16)}, caps ${info.caps}`);

const dir = join('out', 'modules', info.port);
const files = [];
const walk = (d) => {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) walk(p);
    else if (p.endsWith('.dkm')) files.push(p);
  }
};
walk(dir);
const library = (await Promise.all(files.map(async (f) => libraryModule(new Uint8Array(readFileSync(f)))))).filter(
  Boolean,
);
check('the library folder has modules', library.length > 0, `${library.length} in ${relative('.', dir)}`);

const before = await buildView(s, library);
const target = before.modules.find((m) => m.group === 'Machines' && m.on_card === 'no' && !m.test);
check(
  'the list has a machine that is not on the card',
  !!target,
  JSON.stringify(before.modules.map((m) => [m.id, m.on_card, m.loaded])),
);
const events = [];
const out = await install(s, [target.id], library, (e) => events.push(e));
check(
  'install uploaded and verified, selected nothing',
  out.uploaded.length === 1 && out.selected.length === 0 && events.some((e) => e.event === 'verify' && e.ok),
  events.map((e) => e.event).join(' '),
);
const again = [];
await install(s, [target.id], library, (e) => again.push(e));
check(
  'a second install skips the upload',
  again.some((e) => e.event === 'skip') && !again.some((e) => e.event === 'progress'),
);
const after = await buildView(s, library);
check(
  'the list shows it on the card, not loaded',
  after.modules.find((m) => m.id === target.id).on_card === 'yes' &&
    !after.modules.find((m) => m.id === target.id).loaded,
);

if (flags.includes('--restart')) {
  await s.select([target.path]);
  const ev = [];
  const r = await restartDevice(s, (e) => ev.push(e));
  check(
    'restart: the boot is seen and MODULES lists the module',
    r.loaded.includes(target.name),
    `${r.loaded} ${ev.map((e) => e.event)}`,
  );
}
if (flags.includes('--delete')) {
  const del = await deleteFiles(s, [target.path]);
  check(
    'delete: the file is gone',
    del.deleted[0] === target.path && !(await s.list()).some((e) => e.path === target.path),
  );
}
sock.end();
process.exit(checks.every(Boolean) ? 0 : 1);
