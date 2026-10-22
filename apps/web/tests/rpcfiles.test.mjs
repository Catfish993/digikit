// The stock MidiRpc file client (rpcfiles.ts) against a fake Digitakt II that answers the FsSample commands byte for byte (docs/MIDI-SYSEX-RPC.md
// section 12): framing, ReadDir entries, StorageSpace of 17 and 20 bytes, folders, a write in chunks with the byte count at close, one writer at a time (a fake assumption),
// the device's hash over the file plus a 16-byte trailer, and a verify that fails when the device stores something else.
import assert from 'node:assert/strict';
import test from 'node:test';
import { DkSession, decode8in7, encode8in7 } from '../src/modules/dkupload-core.ts';
import { RPC_FILE, RpcFiles, TRAILER, pathBytes, putVerified, verifyWritten } from '../src/modules/rpcfiles.ts';

const concat = (...p) => Uint8Array.from(Buffer.concat(p.map((x) => Buffer.from(x))));
const be32 = (n) => Uint8Array.of((n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255);
const u32 = (b, at) => new DataView(b.buffer, b.byteOffset + at, 4).getUint32(0);
const cstr = (b, at) => {
  const end = b.indexOf(0, at);
  return [Buffer.from(b.subarray(at, end)).toString(), end + 1];
};
/** A stand-in for the +Drive hash: FNV-1a over the bytes, bit 0 set (the client takes the function as an argument). */
const hashOf = (b) => {
  let h = 0x811c9dc5;
  for (const x of b) h = Math.imul(h ^ x, 0x01000193) >>> 0;
  return (h | 1) >>> 0;
};

class FakeDrive {
  constructor({ spaceLen = 17, corrupt = false } = {}) {
    this.dirs = new Set(['/', '/factory']);
    this.files = new Map(); // path -> stored bytes (data + trailer)
    this.writer = null;
    this.next = 6;
    this.queue = [];
    this.seen = [];
    this.spaceLen = spaceLen;
    this.corrupt = corrupt;
    this.requests = [];
  }
  reply(seq, type, payload) {
    const body = concat(seq, seq, Uint8Array.of(type | 0x80), payload);
    this.queue.push(concat(Uint8Array.of(0xf0, 0x00, 0x20, 0x3c, 0x10, 0x00), encode8in7(body), Uint8Array.of(0xf7)));
  }
  send(msg) {
    assert.deepEqual([...msg.subarray(0, 6)], [0xf0, 0x00, 0x20, 0x3c, 0x10, 0x00]);
    const body = decode8in7(msg.subarray(6, msg.length - 1));
    assert.deepEqual([...body.subarray(2, 4)], [0, 0]);
    const seq = body.subarray(0, 2);
    const type = body[4];
    const p = body.subarray(5);
    this.seen.push(type);
    const ok = (...x) => this.reply(seq, type, concat(Uint8Array.of(1), ...x));
    const no = () => this.reply(seq, type, Uint8Array.of(0));
    switch (type) {
      case RPC_FILE.STORAGE_SPACE: {
        const nums = concat(new Uint8Array(4), be32(1000), new Uint8Array(4), be32(5000));
        // 17 bytes: status, free, total; 20 bytes: three more before them
        return this.reply(
          seq,
          type,
          this.spaceLen === 20 ? concat(Uint8Array.of(0, 0, 0, 1), nums) : concat(Uint8Array.of(1), nums),
        );
      }
      case RPC_FILE.READ_DIR: {
        const [dir] = cstr(p, 0);
        const parts = [];
        const under = (path) => path !== dir && path.slice(0, path.lastIndexOf('/') || 1) === (dir === '/' ? '/' : dir);
        for (const d of this.dirs)
          if (under(d))
            parts.push(
              concat(
                be32(0),
                be32(0),
                Uint8Array.of(0, 'D'.charCodeAt(0)),
                Buffer.from(d.slice(d.lastIndexOf('/') + 1) + '\0'),
              ),
            );
        for (const [f, bytes] of this.files)
          if (under(f))
            parts.push(
              concat(
                be32(hashOf(bytes)),
                be32(bytes.length - 16),
                Uint8Array.of(0, 'F'.charCodeAt(0)),
                Buffer.from(f.slice(f.lastIndexOf('/') + 1) + '\0'),
              ),
            );
        return this.reply(seq, type, concat(...parts, new Uint8Array(0)));
      }
      case RPC_FILE.CREATE_DIR: {
        const [path] = cstr(p, 0);
        const parts = path.split('/').filter(Boolean);
        for (let i = 1; i <= parts.length; i++) this.dirs.add('/' + parts.slice(0, i).join('/'));
        return ok();
      }
      case RPC_FILE.DELETE_DIR: {
        const [path] = cstr(p, 0);
        const busy = [...this.files.keys(), ...this.dirs].some((x) => x.startsWith(path + '/'));
        if (!this.dirs.has(path) || busy) return no();
        this.dirs.delete(path);
        return ok();
      }
      case RPC_FILE.DELETE_FILE: {
        const [path] = cstr(p, 0);
        return this.files.delete(path) ? ok() : no();
      }
      case RPC_FILE.RENAME_FILE: {
        const [from, at] = cstr(p, 0);
        const [to] = cstr(p, at);
        if (!this.files.has(from)) return no();
        this.files.set(to, this.files.get(from));
        this.files.delete(from);
        return ok();
      }
      case RPC_FILE.FILE_INFO_V2: {
        const [path] = cstr(p, 0);
        const b = this.files.get(path);
        return this.reply(
          seq,
          type,
          b
            ? concat(Uint8Array.of(1), be32(b.length - 16), be32(hashOf(b)))
            : concat(Uint8Array.of(0), new Uint8Array(8)),
        );
      }
      case RPC_FILE.OPEN_WRITE: {
        const [name] = cstr(p, 4);
        // a bare name is refused (and makes a folder of its name), as the firmware does; so does a second writer
        if (!name.startsWith('/') || this.writer || !this.dirs.has(name.slice(0, name.lastIndexOf('/')) || '/')) {
          if (!name.startsWith('/')) this.dirs.add('/' + name);
          return no();
        }
        this.writer = { handle: this.next++, name, data: [], pos: 0 };
        return ok(be32(this.writer.handle));
      }
      case RPC_FILE.WRITE_V1: {
        const w = this.writer;
        const [handle, len, off] = [u32(p, 0), u32(p, 4), u32(p, 8)];
        if (!w || w.handle !== handle) return this.reply(seq, type, concat(Uint8Array.of(0), be32(0)));
        if (off !== w.pos) return this.reply(seq, type, concat(Uint8Array.of(0), be32(w.pos)));
        w.data.push(Uint8Array.from(p.subarray(12, 12 + len)));
        w.pos += len;
        return ok(be32(w.pos));
      }
      case RPC_FILE.CLOSE_WRITE: {
        const w = this.writer;
        // the byte count is required: a close with the handle alone fails
        if (!w || p.length < 8 || u32(p, 0) !== w.handle || u32(p, 4) !== w.pos)
          return this.reply(seq, type, concat(Uint8Array.of(0), new Uint8Array(8)));
        const stored = concat(...w.data, TRAILER);
        if (this.corrupt) stored[0] ^= 1;
        this.files.set(w.name, stored);
        this.writer = null;
        return ok(be32(w.handle), be32(w.pos));
      }
      default:
        return no();
    }
  }
  async recv() {
    return this.queue.shift() ?? null;
  }
}

const setup = (opts) => {
  const dev = new FakeDrive(opts);
  const session = new DkSession(dev, { timeoutMs: 50 });
  return { dev, session, files: new RpcFiles(session, { chunk: 100, timeoutMs: 50 }) };
};
const data = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 7 + 3) & 255);

test('paths are absolute C-strings under 256 bytes', () => {
  assert.deepEqual([...pathBytes('a/b')], [...Buffer.from('/a/b\0')]);
  assert.deepEqual([...pathBytes('/a')], [...Buffer.from('/a\0')]);
  assert.throws(() => pathBytes('x'.repeat(256)), /too long/);
  assert.doesNotThrow(() => pathBytes('x'.repeat(254)));
  assert.throws(() => pathBytes('a\0b'), /NUL/);
});

test('StorageSpace: 17 bytes, or 20 with the numbers last', async () => {
  for (const spaceLen of [17, 20]) {
    const { files } = setup({ spaceLen });
    assert.deepEqual(await files.space(), { free: 1000n, total: 5000n });
  }
});

test('mkdir -p, ReadDir entries, rename, remove, rmdir', async () => {
  const { files } = setup();
  await files.mkdir('drums/kicks');
  assert.deepEqual(
    (await files.readDir('/')).map((e) => [e.name, e.kind]),
    [
      ['factory', 'D'],
      ['drums', 'D'],
    ],
  );
  assert.deepEqual(
    (await files.readDir('/drums')).map((e) => e.name),
    ['kicks'],
  );
  await files.write('drums/k1', data(10));
  const [k1] = (await files.readDir('/drums')).filter((e) => e.kind === 'F');
  assert.equal(k1.size, 10);
  await files.rename('/drums/k1', '/drums/kicks/k1');
  assert.deepEqual(
    (await files.readDir('/drums/kicks')).map((e) => e.name),
    ['k1'],
  );
  await assert.rejects(files.rmdir('drums/kicks'), /status 0/);
  await files.remove('drums/kicks/k1');
  await files.rmdir('drums/kicks');
  assert.deepEqual(await files.readDir('/drums'), []);
});

test('write: chunks at sequential offsets, the byte count at close, the size without the trailer', async () => {
  const { files, dev } = setup();
  const d = data(250);
  const seen = [];
  const closed = await files.write('s/take', d, (sent, total) => seen.push([sent, total]));
  assert.deepEqual(seen, [
    [100, 250],
    [200, 250],
    [250, 250],
  ]);
  assert.deepEqual(closed, { id: 6, size: 250 });
  assert.equal(dev.seen.filter((t) => t === RPC_FILE.WRITE_V1).length, 3);
  assert.equal(dev.files.get('/s/take').length, 266);
  assert.deepEqual(await files.info('/s/take'), { found: true, size: 250, hash: hashOf(dev.files.get('/s/take')) });
  assert.deepEqual(await files.info('/s/none'), { found: false, size: 0, hash: 0 });
  // an empty file
  assert.equal((await files.write('s/empty', new Uint8Array(0))).size, 0);
});

test('a root-level file is sent with its leading slash (a bare name would fail on the device)', async () => {
  const { files, dev } = setup();
  await files.write('kick', data(8));
  assert.ok(dev.files.has('/kick'));
  assert.ok(!dev.dirs.has('/kick'));
});

test('a failed write closes its writer (the fake allows one at a time) so the next open works', async () => {
  const { files, dev } = setup();
  const real = dev.send.bind(dev);
  let writes = 0;
  dev.send = (msg) => {
    const body = decode8in7(msg.subarray(6, msg.length - 1));
    if (body[4] === RPC_FILE.WRITE_V1 && ++writes === 2) return; // the second chunk is never answered
    real(msg);
  };
  await assert.rejects(files.write('s/lost', data(250)), /no reply/);
  assert.equal(dev.writer, null);
  dev.send = real;
  await files.write('s/next', data(5));
  assert.ok(dev.files.has('/s/next'));
});

test('verify: the device hash covers the stored file and the trailer; a different content fails', async () => {
  const { files } = setup();
  const d = data(300);
  const { closed, verified } = await putVerified(files, 'v/a', d, hashOf);
  assert.equal(closed.size, 300);
  assert.equal(verified.ok, true);
  assert.equal(verified.firmware, verified.host);
  assert.match(verified.compared, /zero trailer/);
  const other = Uint8Array.from(d);
  other[17] ^= 1;
  const bad = await verifyWritten(files, '/v/a', other, hashOf);
  assert.equal(bad.ok, false);
  assert.notEqual(bad.firmware, bad.host);
  const none = await verifyWritten(files, '/v/zzz', d, hashOf);
  assert.equal(none.ok, false);
  assert.equal(none.firmware, null);
});

test('verify: a device that stores other bytes than it was sent fails', async () => {
  const { files } = setup({ corrupt: true });
  const { verified } = await putVerified(files, 'v/b', data(64), hashOf);
  assert.equal(verified.ok, false);
  assert.equal(verified.size, 64);
});
