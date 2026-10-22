// The pure TypeScript protocol client (`DkSession`, dkupload-core.ts) and the Transfer logic (dktransfer.ts) against a fake device that
// speaks the SysEx protocol of docs/design/digikit-upload.md byte for byte (framing included), as the Rust mock does for the wasm client.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import {
  DkError,
  DkSession,
  decode8in7,
  encode8in7,
  frame,
  parseReply,
  restarted,
  splitMessages,
} from '../src/modules/dkupload-core.ts';
import { CAP, CAPACITY_KINDS, CMD, LOG_KIND } from '../src/modules/gen_proto.ts';
import {
  PROGRESS_EVENTS,
  buildView,
  deleteFiles,
  headerInfo,
  install,
  libraryModule,
  mergeView,
  restartDevice,
  uploadProgress,
} from '../src/modules/dktransfer.ts';

const sha = (b) => createHash('sha256').update(b).digest();
const hex = (b) => Buffer.from(b).toString('hex');
const concat = (...p) => Uint8Array.from(Buffer.concat(p.map((x) => Buffer.from(x))));
const be32 = (n) => Uint8Array.of((n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255);
const cs = (s) => concat(Buffer.from(s), Uint8Array.of(0));

/** A .dkm with a valid header (docs/design/digikit-dkm.md) of `size` bytes. */
function dkm(name, { machine = 0, fx = 0, service = 1, size = 700, dsp = 0, version = 0x000102, salt = 0 } = {}) {
  const b = new Uint8Array(size);
  b.set([0x44, 0x4b, 0x4d, 0x46], 0);
  new DataView(b.buffer).setUint32(8, size);
  new DataView(b.buffer).setUint32(0x34, version);
  b.set([1, 1, machine, 0, fx, 0, service, 0], 0x38);
  b.set(Buffer.from(name), 0x40);
  new DataView(b.buffer).setUint32(0x64, 100);
  new DataView(b.buffer).setUint32(0x68, 20);
  new DataView(b.buffer).setUint32(0x9c, dsp);
  for (let i = 0xa0; i < size; i++) b[i] = (i * 31 + salt) & 255;
  return b;
}

const ST = { E_STATE: -53, E_NOFILE: -59, E_HASH: -56, E_FULL: -52, E_OFFSET: -54 };

/** The device: files, a selection, a log, modules, and a RESTART that is silent for a while and then boots again. */
class FakeDevice {
  constructor() {
    this.files = new Map();
    this.dirs = new Set(); // folders with nothing in them
    this.selection = [];
    this.seen = []; // the commands received, by name
    this.corrupt = false; // VERIFY answers a wrong hash
    this.pageSize = 2; // LIST entries per reply
    this.silent = 0; // requests to ignore (the reboot)
    this.loaded = []; // the names MODULES lists
    this.staged = null;
    this.log = [{ seq: 1, kind: LOG_KIND.BOOT_START, flags: 0, args: [516, 2, 0, 0, 0] }];
    this.queue = [];
    this.free = 1 << 20;
    this.pending = 0; // after a RESTART: requests that are still answered by the old boot before the reset
  }
  reply(cmd, seq, status, body = new Uint8Array()) {
    const packed = encode8in7(concat(Uint8Array.of(status & 255), body));
    return concat([0xf0, 0x7d, 0x64, 0x6b, 1, cmd | 0x40, seq], packed, [0xf7]);
  }
  send(msg) {
    const cmd = msg[5];
    const seq = msg[6];
    const body = decode8in7(msg.subarray(7, msg.length - 1));
    if (this.silent > 0) {
      this.silent--;
      if (this.silent === 0) this.boot();
      return;
    }
    const name = Object.entries(CMD).find(([, v]) => v === cmd)?.[0] ?? `0x${cmd.toString(16)}`;
    this.seen.push(name);
    const r = this.handle(cmd, seq, body);
    if (r) this.queue.push(r);
    if (this.pending > 0 && cmd !== CMD.RESTART && --this.pending === 0) this.silent = 2; // the guest is resetting: two requests get no answer
  }
  boot() {
    this.log = [{ seq: 1, kind: LOG_KIND.BOOT_START, flags: 0, args: [516, 2, 0, 0, 0] }];
  }
  async recv() {
    return this.queue.shift() ?? null;
  }
  pathOf(body) {
    const end = body.indexOf(0);
    return [Buffer.from(body.subarray(0, end)).toString(), end + 1];
  }
  handle(cmd, seq, body) {
    const ok = (b) => this.reply(cmd, seq, 0, b);
    const err = (code, b) => this.reply(cmd, seq, code, b);
    switch (cmd) {
      case CMD.INFO: {
        const flags = this.staged ? 2 : 0;
        return ok(
          concat(
            Uint8Array.of(0, 1, 0x02, 0x04),
            cs('dt2'),
            be32(this.free),
            be32(32768),
            [flags],
            be32(1),
            be32(this.log.length),
            be32(0x000204),
            be32(Object.values(CAP).reduce((a, bit) => a | (1 << bit), 0)),
          ),
        );
      }
      case CMD.LIST: {
        const first = new DataView(body.buffer, body.byteOffset).getUint32(0);
        const prefix = body.length > 4 ? Buffer.from(body.subarray(4, body.length - 1)).toString() : '';
        const all = [...this.files, ...[...this.dirs].map((d) => [d, new Uint8Array(0), true])]
          .filter(([p]) => p.startsWith(prefix))
          .sort(([a], [b]) => (a < b ? -1 : 1));
        const page = all.slice(first, first + this.pageSize);
        return ok(
          concat(
            be32(all.length),
            [page.length],
            ...page.map(([p, d, dir]) => concat(cs(p), be32(d.length), dir ? new Uint8Array(32) : sha(d))),
          ),
        );
      }
      case CMD.MKDIR: {
        const [path] = this.pathOf(body);
        if ([...this.files.keys()].some((f) => f === path || path.startsWith(`${f}/`))) return err(-51);
        const exists = [...this.dirs, ...this.files.keys()].some((f) => f === path || f.startsWith(`${path}/`));
        if (!exists) {
          for (const d of [...this.dirs]) if (path.startsWith(`${d}/`)) this.dirs.delete(d);
          this.dirs.add(path);
        }
        return ok();
      }
      case CMD.BEGIN: {
        const [path, at] = this.pathOf(body);
        const size = new DataView(body.buffer, body.byteOffset).getUint32(at);
        this.staged = { path, size, sha: body.slice(at + 4, at + 36), data: new Uint8Array(0), last: -1 };
        return ok();
      }
      case CMD.DATA: {
        const st = this.staged;
        if (!st) return err(ST.E_STATE);
        const off = new DataView(body.buffer, body.byteOffset).getUint32(0);
        const part = body.subarray(4);
        if (off === st.last) return ok(); // the repeat of the last chunk
        if (off !== st.data.length) return err(ST.E_OFFSET);
        st.data = concat(st.data, part);
        st.last = off;
        return ok();
      }
      case CMD.END: {
        const st = this.staged;
        if (!st) return err(ST.E_STATE);
        this.staged = null;
        if (st.data.length !== st.size || hex(sha(st.data)) !== hex(st.sha)) return err(ST.E_HASH);
        for (const d of [...this.dirs]) if (st.path.startsWith(`${d}/`)) this.dirs.delete(d);
        this.files.set(st.path, st.data);
        return ok();
      }
      case CMD.DELETE: {
        const [path] = this.pathOf(body);
        return this.files.delete(path) || this.dirs.delete(path) ? ok() : err(ST.E_NOFILE);
      }
      case CMD.READ: {
        const [path, at] = this.pathOf(body);
        const f = this.files.get(path);
        if (!f) return err(ST.E_NOFILE);
        const dv = new DataView(body.buffer, body.byteOffset);
        const off = dv.getUint32(at);
        const len = Math.min(dv.getUint32(at + 4), 768, f.length - off);
        return ok(concat(be32(f.length), f.subarray(off, off + len)));
      }
      case CMD.VERIFY: {
        const [path, at] = this.pathOf(body);
        const f = this.files.get(path);
        if (!f) return err(ST.E_NOFILE);
        if (body.length > at) {
          const dv = new DataView(body.buffer, body.byteOffset);
          const off = dv.getUint32(at);
          const len = dv.getUint32(at + 4);
          if (off % 512 !== 0 || off >= f.length) return err(ST.E_OFFSET);
          if (len === 0 || len > f.length - off) return err(-55);
          return ok(sha(f.subarray(off, off + len)));
        }
        return ok(this.corrupt ? sha(concat(f, [1])) : sha(f));
      }
      case CMD.SELECT: {
        const n = body[0];
        let at = 1;
        this.selection = [];
        for (let i = 0; i < n; i++) {
          const [p, used] = this.pathOf(body.subarray(at));
          this.selection.push(p);
          at += used;
        }
        return ok();
      }
      case CMD.RESTART:
        this.log.push({ seq: this.log.length + 1, kind: LOG_KIND.UPLOAD, flags: 0, args: [CMD.RESTART, 0, 0, 0, 0] });
        this.pending = 1;
        this.loaded = this.selection.map((p) => `mod:${p}`);
        return ok();
      case CMD.MODULES: {
        const entry = (name, i) => {
          const nm = new Uint8Array(32);
          nm.set(Buffer.from(name));
          return concat(
            [i],
            nm,
            be32(0x10000),
            [1, 1, 1, 0, 0, 0, 1, 0],
            be32(0x47e8b000),
            be32(256),
            be32(0),
            be32(0),
            be32(1),
            [1, 17],
            be32(0),
            [0],
          );
        };
        return ok(
          concat(
            be32(100),
            be32(90),
            be32(80),
            be32(70),
            [this.loaded.length, this.loaded.length],
            ...this.loaded.map(entry),
          ),
        );
      }
      case CMD.CAPACITY:
        return ok(
          concat(
            [1, 3],
            [CAPACITY_KINDS.MODULES],
            be32(8),
            be32(this.loaded.length),
            be32(0),
            [CAPACITY_KINDS.ARENA],
            be32(1000),
            be32(0),
            be32(300),
            [CAPACITY_KINDS.DSP_AREA],
            be32(1000),
            be32(0),
            be32(50),
          ),
        );
      case CMD.LOG: {
        const from = new DataView(body.buffer, body.byteOffset).getUint32(0);
        const ev = this.log.filter((e) => e.seq >= from).slice(0, 3);
        const out = ev.map((e) =>
          concat(be32(e.seq), be32(0), [e.kind >> 8, e.kind & 255, e.flags >> 8, e.flags & 255], ...e.args.map(be32)),
        );
        return ok(concat(be32(1), be32(this.log.length), [ev.length], ...out));
      }
      default:
        return err(-60);
    }
  }
}

const session = (dev, o = {}) => new DkSession(dev, { timeoutMs: 20, retries: 1, ...o });
const sleep = async () => {};

test('framing: the vectors of the spec, round trips, partial groups, message splitting', () => {
  // `80 01 02 03 04 05 06` packs to `40 00 01 02 03 04 05 06`; `FF` packs to `40 7F` (tests/digikit/test_dkupload.py has the same)
  assert.equal(hex(frame(CMD.DATA, 5, Uint8Array.from([0x80, 1, 2, 3, 4, 5, 6]))), 'f07d646b0111054000010203040506f7');
  assert.equal(hex(frame(CMD.INFO, 1, Uint8Array.of(0xff))).slice(14, -2), '407f');
  for (const n of [0, 1, 6, 7, 8, 13, 14, 15, 100]) {
    const b = Uint8Array.from({ length: n }, (_, i) => (i * 97 + 13) & 255);
    assert.deepEqual(decode8in7(encode8in7(b)), b, `${n} bytes`);
  }
  const reply = concat(
    [0xf0, 0x7d, 0x64, 0x6b, 1, CMD.DELETE | 0x40, 9],
    encode8in7(Uint8Array.of(0xc5, 0x12, 0x80)),
    [0xf7],
  );
  const r = parseReply(reply);
  assert.deepEqual([r.cmd, r.seq, r.status, [...r.body]], [CMD.DELETE, 9, -59, [0x12, 0x80]]);
  assert.equal(parseReply(frame(CMD.INFO, 1)), null); // a request is not a reply
  assert.equal(parseReply(Uint8Array.from([0xf0, 0, 0x20, 0x3c, 0x10, 0, 1, 0, 0xf7])), null); // the stock MidiRpc
  const s = splitMessages(Uint8Array.from([0x90, 1, 2, 0xf0, 1, 0xf7, 0xf0, 2, 3]));
  assert.deepEqual(
    s.messages.map((m) => [...m]),
    [[0xf0, 1, 0xf7]],
  );
  assert.deepEqual([...s.rest], [0xf0, 2, 3]);
});

test('session: info with caps, list in pages, modules, capacity, read, verify, select', async () => {
  const dev = new FakeDevice();
  for (const [i, n] of ['modules/a.dkm', 'modules/b.dkm', 'modules/c.dkm'].entries())
    dev.files.set(n, dkm(`x/${i}`, { salt: i }));
  dev.loaded = ['digikit/pluck'];
  const s = session(dev);
  const info = await s.info();
  assert.deepEqual(
    [info.port, info.runtime, info.max_chunk, info.safe_mode, info.card_read_only],
    ['dt2', 0x204, 32768, false, false],
  );
  assert.ok(info.caps !== null);
  const ls = await s.list();
  assert.deepEqual(
    ls.map((e) => e.path),
    ['modules/a.dkm', 'modules/b.dkm', 'modules/c.dkm'],
  ); // three entries, two per page
  assert.equal(ls[0].sha256, hex(sha(dev.files.get('modules/a.dkm'))));
  assert.deepEqual(
    (await s.modules()).map((m) => [m.name, m.types, m.contracts[1]]),
    [['digikit/pluck', [17], [1, 0]]],
  );
  assert.deepEqual(await s.capacity(), {
    MODULES: { limit: 8, used: 1, largest: 0 },
    ARENA: { limit: 1000, used: 0, largest: 300 },
    DSP_AREA: { limit: 1000, used: 0, largest: 50 },
  });
  const h = await s.header('modules/b.dkm');
  assert.equal(h.length, 0xa0);
  assert.deepEqual(h, dev.files.get('modules/b.dkm').subarray(0, 0xa0));
  assert.equal(await s.verify('modules/b.dkm'), hex(sha(dev.files.get('modules/b.dkm'))));
  await s.select(['modules/a.dkm', 'modules/c.dkm']);
  assert.deepEqual(dev.selection, ['modules/a.dkm', 'modules/c.dkm']);
  await assert.rejects(s.verify('modules/none.dkm'), (e) => e instanceof DkError && e.status === 'E_NOFILE');
  await assert.rejects(s.select(Array(9).fill('modules/x.dkm')), /at most 8/);
});

test('session: a timeout sends the request again; upload in chunks with progress, a repeated DATA, E_HASH', async () => {
  const dev = new FakeDevice();
  // the first request gets no answer: sent again with a new seq
  dev.silent = 1;
  const s = session(dev, { chunk: 512 });
  assert.equal((await s.info()).port, 'dt2');
  await assert.rejects(
    session(Object.assign(new FakeDevice(), { silent: 9 })).info(),
    (e) => e instanceof DkError && e.kind === 'timeout',
  );
  const data = dkm('x/big', { size: 2000 });
  const seen = [];
  await s.put('modules/big.dkm', data, (sent, total) => seen.push([sent, total]));
  assert.deepEqual(seen, [
    [512, 2000],
    [1024, 2000],
    [1536, 2000],
    [2000, 2000],
  ]);
  assert.deepEqual(dev.files.get('modules/big.dkm'), data);
  // a file that is not what BEGIN promised: the device says E_HASH (a repeated chunk would be answered ok)
  const bad = new Uint8Array(1000).fill(7);
  const dev2 = new FakeDevice();
  const orig = dev2.handle.bind(dev2);
  dev2.handle = (cmd, seq, body) => {
    if (cmd === CMD.DATA && new DataView(body.buffer, body.byteOffset).getUint32(0) === 0)
      body = concat(body.subarray(0, 4), new Uint8Array(512).fill(9));
    return orig(cmd, seq, body);
  };
  await assert.rejects(
    session(dev2, { chunk: 512 }).put('modules/bad.dkm', bad),
    (e) => e instanceof DkError && e.status === 'E_HASH',
  );
  assert.equal(dev2.files.size, 0);
  assert.ok(dev2.seen.includes('ABORT')); // after an error the upload is dropped
});

test('restarted(): the log of the old boot holds the RESTART request, the new boot starts with BOOT_START', () => {
  const boot = { seq: 1, kind: LOG_KIND.BOOT_START, flags: 0, args: [4, 0, 0, 0, 0] };
  const asked = { seq: 2, kind: LOG_KIND.UPLOAD, flags: 0, args: [CMD.RESTART, 0, 0, 0, 0] };
  assert.equal(restarted([boot, { ...asked, args: [CMD.SELECT, 0, 0, 0, 0] }]), true);
  assert.equal(restarted([boot, asked]), false);
  assert.equal(restarted([]), false);
  assert.throws(
    () => restarted([boot, asked, { ...asked, flags: 1, args: [CMD.RESTART, -43 >>> 0, 0, 0, 0] }]),
    /refused/,
  );
});

test('header, library module, the merged list', async () => {
  const pluck = dkm('digikit/pluck', { machine: 1, dsp: 64, size: 900 });
  const sine = dkm('test/sine', { machine: 1, dsp: 400, size: 800 });
  const tool = dkm('digikit/tool', { service: 1, size: 500 });
  assert.deepEqual(
    [headerInfo(pluck).group, headerInfo(pluck).version, headerInfo(pluck).arena, headerInfo(pluck).dsp],
    ['Machines', '0.1.2', 120, 64],
  );
  assert.equal(headerInfo(dkm('a', { fx: 1 })).group, 'Effects');
  assert.equal(headerInfo(tool).group, 'Tools');
  assert.equal(headerInfo(new Uint8Array(300)), null);
  assert.equal(await libraryModule(new Uint8Array(300)), null);
  const lm = await libraryModule(pluck);
  assert.deepEqual(
    [lm.id, lm.label, lm.test, lm.path, lm.file_size, lm.sha256],
    ['digikit/pluck', 'pluck', false, 'modules/pluck.dkm', 900, hex(sha(pluck))],
  );

  const library = [await libraryModule(sine), lm, await libraryModule(tool)];
  const card = [
    { path: 'modules/pluck.dkm', size: 900, sha256: hex(sha(pluck)) }, // same file
    { path: 'modules/tool.dkm', size: 500, sha256: 'ff' }, // same path, other bytes
    { path: 'modules/other.dkm', size: 700, sha256: 'aa' }, // not from the library
    { path: 'modules/junk.dkm', size: 9, sha256: 'bb' },
  ];
  const headers = { 'modules/other.dkm': dkm('someone/effect', { fx: 1, size: 700 }), 'modules/junk.dkm': null };
  const cap = {
    MODULES: { limit: 8, used: 1, largest: 0 },
    ARENA: { limit: 1000, used: 0, largest: 300 },
    DSP_AREA: { limit: 1000, used: 0, largest: 50 },
  };
  const v = mergeView(library, card, ['digikit/pluck', 'someone/effect'], cap, headers);
  assert.deepEqual(
    v.modules.map((r) => r.id),
    ['digikit/pluck', 'test/sine', 'digikit/tool'],
  ); // group, test last, id
  const by = Object.fromEntries(v.modules.map((r) => [r.id, r]));
  assert.deepEqual(
    [by['digikit/pluck'].on_card, by['digikit/pluck'].loaded, by['digikit/pluck'].fits],
    ['yes', true, true],
  );
  assert.equal(by['digikit/tool'].on_card, 'other version');
  assert.deepEqual([by['test/sine'].on_card, by['test/sine'].fits, by['test/sine'].test], ['no', false, true]); // DSP 400 > largest 50
  assert.match(by['test/sine'].warnings[0], /DSP area/);
  const extra = Object.fromEntries(v.card_only.map((r) => [r.path, r]));
  assert.deepEqual(
    [extra['modules/other.dkm'].group, extra['modules/other.dkm'].loaded, extra['modules/other.dkm'].name],
    ['Effects', true, 'someone/effect'],
  );
  assert.deepEqual([extra['modules/junk.dkm'].known, extra['modules/junk.dkm'].group], [false, 'Tools']);
  assert.equal(mergeView(library, [], [], null, {}).modules[0].fits, null); // no CAPACITY: no verdict
});

test('buildView reads the device: INFO, LIST, MODULES, CAPACITY and the headers of the card-only files by READ', async () => {
  const dev = new FakeDevice();
  const pluck = dkm('digikit/pluck', { machine: 1, dsp: 64, size: 900 });
  dev.files.set('modules/pluck.dkm', pluck);
  dev.files.set('modules/other.dkm', dkm('someone/effect', { fx: 1, size: 700 }));
  dev.loaded = ['digikit/pluck'];
  const view = await buildView(session(dev), [await libraryModule(pluck)]);
  assert.deepEqual(
    [view.device, view.runtime, view.free_bytes, view.loaded],
    ['dt2', '0.2.4', 1 << 20, ['digikit/pluck']],
  );
  assert.equal(view.modules[0].on_card, 'yes');
  assert.deepEqual([view.card_only[0].group, view.card_only[0].version], ['Effects', '0.1.2']);
  assert.ok(dev.seen.includes('READ') && dev.seen.includes('MODULES') && dev.seen.includes('CAPACITY'));
});

test('install: skip the same sha256, upload the rest with capped progress, VERIFY each, no SELECT', async () => {
  const dev = new FakeDevice();
  const pluck = dkm('digikit/pluck', { machine: 1, size: 900 });
  const big = dkm('test/big', { size: 20000 });
  dev.files.set('modules/pluck.dkm', pluck); // already on the card
  const events = [];
  const out = await install(
    session(dev, { chunk: 512 }),
    ['digikit/pluck', 'test/big'],
    [await libraryModule(pluck), await libraryModule(big)],
    (e) => events.push(e),
  );
  assert.deepEqual(out, { uploaded: ['test/big'], selected: [], restarted: false });
  assert.deepEqual(dev.files.get('modules/big.dkm') ?? dev.files.get('modules/big.dkm'), big);
  assert.ok(!dev.seen.includes('SELECT') && !dev.seen.includes('RESTART'));
  assert.equal(dev.seen.filter((c) => c === 'VERIFY').length, 2);
  const kinds = events.map((e) => e.event);
  assert.deepEqual(kinds.slice(0, 4), ['start', 'upload', 'skip', 'verify']); // the skipped file jumps straight to its verify
  assert.equal(kinds.at(-1), 'done');
  assert.deepEqual(events.at(-1), { event: 'done', restarted: false, selected: [] });
  const prog = events.filter((e) => e.event === 'progress');
  assert.ok(prog.length > 1 && prog.length <= PROGRESS_EVENTS + 1, String(prog.length)); // 40 chunks, a few events
  assert.deepEqual([prog.at(-1).sent, prog.at(-1).bytes], [20000, 20000]);
  assert.ok(prog.every((e) => e.id === 'test/big'));
  assert.deepEqual(
    events.filter((e) => e.event === 'verify').map((e) => e.ok),
    [true, true],
  );
  await assert.rejects(install(session(dev), ['digikit/none'], [await libraryModule(pluck)]), /not a run-time module/);
});

test('install: a VERIFY that does not match is an error', async () => {
  const dev = new FakeDevice();
  dev.corrupt = true;
  const m = dkm('digikit/pluck', { machine: 1 });
  const events = [];
  await assert.rejects(
    install(session(dev), ['digikit/pluck'], [await libraryModule(m)], (e) => events.push(e)),
    /did not keep the file/,
  );
  assert.deepEqual(events.at(-1), { event: 'verify', id: 'digikit/pluck', path: 'modules/pluck.dkm', ok: false });
});

test('the upload progress callback: at most a tenth apart, always the last', () => {
  const ev = [];
  const cb = uploadProgress((e) => ev.push(e), 'a/b', 700);
  cb(700, 700);
  assert.deepEqual(ev, [{ event: 'progress', id: 'a/b', sent: 700, bytes: 700 }]);
  const many = [];
  const cb2 = uploadProgress((e) => many.push(e), 'a/c', 5);
  for (let n = 1; n <= 5; n++) cb2(n, 5);
  assert.equal(many.at(-1).sent, 5);
  assert.ok(many.length <= PROGRESS_EVENTS + 1);
});

test('delete: one DELETE per path, confirmed by LIST; a path that is not on the card sends nothing', async () => {
  const dev = new FakeDevice();
  for (const n of ['a', 'b', 'c']) dev.files.set(`modules/${n}.dkm`, dkm(`x/${n}`));
  const events = [];
  const out = await deleteFiles(session(dev), ['modules/a.dkm', 'modules/c.dkm', 'modules/a.dkm'], (e) =>
    events.push(e),
  );
  assert.deepEqual(out, { deleted: ['modules/a.dkm', 'modules/c.dkm'] }); // a repeated path is one DELETE
  assert.deepEqual([...dev.files.keys()], ['modules/b.dkm']);
  assert.equal(dev.seen.filter((c) => c === 'DELETE').length, 2);
  assert.ok(!dev.seen.includes('SELECT')); // the selection is the device's
  assert.deepEqual(
    events.map((e) => e.event),
    ['start', 'delete', 'delete', 'done'],
  );
  assert.deepEqual(events.at(-1), { event: 'done', deleted: ['modules/a.dkm', 'modules/c.dkm'] });
  const before = dev.seen.length;
  const dev2 = new FakeDevice();
  dev2.files.set('modules/a.dkm', dkm('x/a'));
  await assert.rejects(
    deleteFiles(session(dev2), ['modules/a.dkm', 'modules/none.dkm']),
    /not on the card: modules\/none\.dkm/,
  );
  assert.ok(!dev2.seen.includes('DELETE'));
  assert.ok(before > 0);
  // the card keeps the file after DELETE: error
  const dev3 = new FakeDevice();
  dev3.files.set('modules/a.dkm', dkm('x/a'));
  const orig = dev3.handle.bind(dev3);
  dev3.handle = (cmd, seq, body) => (cmd === CMD.DELETE ? dev3.reply(cmd, seq, 0) : orig(cmd, seq, body));
  await assert.rejects(deleteFiles(session(dev3), ['modules/a.dkm']), /still on the card after DELETE/);
});

test('restart: RESTART, wait for the new boot in the log, then report what MODULES lists', async () => {
  const dev = new FakeDevice();
  dev.selection = ['modules/pluck.dkm'];
  const events = [];
  const out = await restartDevice(session(dev, { timeoutMs: 5, retries: 0 }), (e) => events.push(e), sleep);
  assert.deepEqual(out, { loaded: ['mod:modules/pluck.dkm'], missing: [] });
  assert.deepEqual(
    events.map((e) => e.event),
    ['restart', 'wait', 'done'],
  );
  assert.deepEqual(events.at(-1), { event: 'done', restarted: true, loaded: ['mod:modules/pluck.dkm'], missing: [] });
  // the old boot's log (with the RESTART request) answered first, then two requests got nothing, then the new boot
  assert.ok(dev.seen.filter((c) => c === 'LOG').length >= 2);
  assert.equal(dev.log.length, 1);
  assert.equal(dev.log[0].kind, LOG_KIND.BOOT_START);
});

test('LIST by prefix pages through the matches; MKDIR makes a folder entry that a file replaces', async () => {
  const dev = new FakeDevice();
  const s = session(dev);
  await s.put('modules/a.dkm', dkm('a'));
  await s.put('samples/one.wav', new Uint8Array(1500).fill(1));
  await s.put('samples/two.wav', new Uint8Array(700).fill(2));
  await s.put('samples/three.wav', new Uint8Array(900).fill(3));
  await s.mkdir('samples/empty');
  await s.mkdir('samples/empty'); // exists: ok
  const under = await s.list('samples/');
  assert.deepEqual(
    under.map((e) => [e.path, e.size]),
    [
      ['samples/empty', 0],
      ['samples/one.wav', 1500],
      ['samples/three.wav', 900],
      ['samples/two.wav', 700],
    ],
  );
  assert.equal(under[0].sha256, '0'.repeat(64));
  assert.equal((await s.list()).length, 5);
  await s.put('samples/empty/x.wav', new Uint8Array(512));
  assert.deepEqual(
    (await s.list('samples/empty')).map((e) => e.path),
    ['samples/empty/x.wav'],
  );
  await assert.rejects(s.mkdir('modules/a.dkm'), (e) => e instanceof DkError && e.status === 'E_PATH');
  await s.delete('samples/one.wav');
  assert.equal((await s.list('samples/')).length, 3);
});

test('VERIFY of a range, and verifyFile in pieces', async () => {
  const dev = new FakeDevice();
  const s = session(dev);
  const data = new Uint8Array(5000).map((_, i) => (i * 7) & 255);
  await s.put('samples/a.wav', data);
  assert.equal(await s.verify('samples/a.wav'), hex(sha(data)));
  assert.equal(await s.verify('samples/a.wav', 1024, 2000), hex(sha(data.subarray(1024, 3024))));
  assert.equal(await s.verifyFile('samples/a.wav', data, 1024), true);
  const other = data.slice();
  other[4999] ^= 1;
  assert.equal(await s.verifyFile('samples/a.wav', other, 1024), false);
  await assert.rejects(s.verify('samples/a.wav', 100, 10), (e) => e instanceof DkError && e.status === 'E_OFFSET');
  assert.equal(await s.verifyFile('samples/a.wav', new Uint8Array(0), 1024), false); // the file is not empty
});
