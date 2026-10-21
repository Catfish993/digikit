// The module upload protocol (docs/design/digikit-upload.md) in the browser: a thin wrapper over dkupload.wasm
// (packages/dkupload/src/wasm.rs, the same codec and upload state machine the desktop app runs natively) and an
// async driver over a transport. Written without TS-only runtime syntax so node can import it for the tests.
//
// Two clients live here. `DkClient` wraps the wasm codec (the web page). `DkSession`, at the end of the file, is pure TypeScript:
// the same framing and every command of the protocol (INFO, LIST, upload, DELETE, CLEAR, ABORT, MODULES, CAPACITY, READ, VERIFY, SELECT,
// RESTART, LOG), with the numbers generated from sdk/sdk.toml (`gen_proto.ts`). The desktop app uses it (no wasm, no Python).
import {
  CAP as GCAP,
  CAPACITY_KINDS,
  CMD as GCMD,
  LOAD_STATUS,
  LOG_KIND,
  PROTO_VERSION,
  STATUS as GSTATUS,
} from './gen_proto.ts';

export interface DkInfo {
  protocol: number;
  runtime_version: number;
  port: string;
  free_bytes: number;
  max_chunk: number;
  safe_mode: boolean;
  upload_open: boolean;
}
export interface DkEntry {
  path: string;
  size: number;
  /** hex */
  sha256: string;
}
export interface DkReply {
  cmd: number;
  seq: number;
  status: number;
  name: string;
  info?: DkInfo;
  list?: { total: number; entries: DkEntry[] };
}
export interface DkProgress {
  done: number;
  total: number;
}
/** Send one whole SysEx message; receive the next one from the device, or null after the timeout. */
export interface DkTransport {
  send(msg: Uint8Array): void | Promise<void>;
  recv(timeoutMs: number): Promise<Uint8Array | null>;
}

export class DkError extends Error {
  kind: string;
  /** The spec's status name (`E_FULL`) for a device error. */
  status?: string;
  freeBytes?: number;
  constructor(j: { error: string; kind: string; status?: string; free_bytes?: number | null }) {
    super(j.error);
    this.kind = j.kind;
    this.status = j.status;
    this.freeBytes = j.free_bytes ?? undefined;
  }
}

export const CMD = {
  INFO: 0x01,
  LIST: 0x02,
  BEGIN: 0x10,
  DATA: 0x11,
  END: 0x12,
  DELETE: 0x13,
  CLEAR: 0x14,
  ABORT: 0x15,
};
export const MAX_CHUNK = 32768;

interface Exports {
  memory: WebAssembly.Memory;
  dk_alloc(n: number): number;
  dk_free(p: number, n: number): void;
  dk_cmd(cmd: number, seq: number, a: number, aLen: number, num: number): number;
  dk_decode(m: number, n: number): number;
  dk_put_new(path: number, pathLen: number, data: number, dataLen: number, chunk: number, seq: number): number;
  dk_put_next(h: number): number;
  dk_put_reply(h: number, m: number, n: number): number;
  dk_put_timeout(h: number): number;
  dk_put_state(h: number): number;
  dk_put_free(h: number): void;
  dk_mock_new(capacity: number): number;
  dk_mock_handle(h: number, m: number, n: number): number;
  dk_mock_ctl(h: number, kind: number, n: number): void;
  dk_mock_files(h: number): number;
  dk_out_ptr(): number;
  dk_out_len(): number;
  dk_err_ptr(): number;
  dk_err_len(): number;
}

export class Dk {
  private x: Exports;
  private constructor(x: Exports) {
    this.x = x;
  }
  static async create(wasm: BufferSource): Promise<Dk> {
    const { instance } = await WebAssembly.instantiate(wasm, {});
    return new Dk(instance.exports as unknown as Exports);
  }

  private bytes(ptr: number, len: number): Uint8Array {
    return new Uint8Array(this.x.memory.buffer.slice(ptr, ptr + len));
  }
  private out(): Uint8Array {
    return this.bytes(this.x.dk_out_ptr(), this.x.dk_out_len());
  }
  private json<T>(): T {
    return JSON.parse(new TextDecoder().decode(this.out())) as T;
  }
  private fail(): never {
    const text = new TextDecoder().decode(this.bytes(this.x.dk_err_ptr(), this.x.dk_err_len()));
    throw new DkError(JSON.parse(text));
  }
  /** Run `f` with the bytes copied into wasm memory. */
  private with<T>(blocks: Uint8Array[], f: (...ptrs: number[]) => T): T {
    const ptrs = blocks.map((b) => {
      const p = this.x.dk_alloc(b.length);
      new Uint8Array(this.x.memory.buffer, p, b.length).set(b);
      return p;
    });
    try {
      return f(...ptrs);
    } finally {
      blocks.forEach((b, i) => this.x.dk_free(ptrs[i], b.length));
    }
  }

  /** INFO (1), LIST (2, `num` = first index), DELETE (0x13, `path`), CLEAR (0x14) or ABORT (0x15) as a SysEx message. */
  command(cmd: number, seq: number, path = '', num = 0): Uint8Array {
    const a = new TextEncoder().encode(path);
    return this.with([a], (p) => {
      if (this.x.dk_cmd(cmd, seq, p, a.length, num) !== 0) this.fail();
      return this.out();
    });
  }
  /** A reply as JSON. Throws `DkError` for a non-zero status; null when the bytes are not a digikit reply. */
  decode(msg: Uint8Array): DkReply | null {
    return this.with([msg], (p) => {
      if (this.x.dk_decode(p, msg.length) !== 0) return null;
      const j = this.json<DkReply & { error?: string; kind?: string }>();
      if (j.error) throw new DkError(j as unknown as ConstructorParameters<typeof DkError>[0]);
      return j;
    });
  }

  putNew(path: string, data: Uint8Array, seq: number, chunk = MAX_CHUNK): number {
    const p = new TextEncoder().encode(path);
    return this.with([p, data], (a, b) => {
      const h = this.x.dk_put_new(a, p.length, b, data.length, chunk, seq);
      if (h === 0) this.fail();
      return h;
    });
  }
  /** The next message to send, or null. */
  putNext(h: number): Uint8Array | null {
    return this.x.dk_put_next(h) === 1 ? this.out() : null;
  }
  putReply(h: number, msg: Uint8Array): void {
    this.with([msg], (p) => {
      if (this.x.dk_put_reply(h, p, msg.length) !== 0) this.fail();
    });
  }
  putTimeout(h: number): void {
    if (this.x.dk_put_timeout(h) !== 0) this.fail();
  }
  putState(h: number): {
    phase: string;
    done: number;
    total: number;
    seq: number;
    end_retried: boolean;
    sha256: string;
  } {
    if (this.x.dk_put_state(h) !== 0) this.fail();
    return this.json();
  }
  putFree(h: number): void {
    this.x.dk_put_free(h);
  }

  mockNew(capacity: number): number {
    return this.x.dk_mock_new(capacity);
  }
  mockHandle(h: number, msg: Uint8Array): Uint8Array | null {
    return this.with([msg], (p) => (this.x.dk_mock_handle(h, p, msg.length) === 1 ? this.out() : null));
  }
  /** kind: 0 drop the next n requests, 1 drop the next n replies, 2 no store, 3 safe mode. */
  mockCtl(h: number, kind: number, n: number): void {
    this.x.dk_mock_ctl(h, kind, n);
  }
  mockFiles(h: number): DkEntry[] {
    this.x.dk_mock_files(h);
    return this.json();
  }
}

/** A transport onto the wasm mock device: a lost message shows as a timeout at once. */
export class MockTransport implements DkTransport {
  dk: Dk;
  handle: number;
  private queue: Uint8Array[] = [];
  constructor(dk: Dk, capacity: number) {
    this.dk = dk;
    this.handle = dk.mockNew(capacity);
  }
  send(msg: Uint8Array): void {
    const r = this.dk.mockHandle(this.handle, msg);
    if (r) this.queue.push(r);
  }
  async recv(): Promise<Uint8Array | null> {
    return this.queue.shift() ?? null;
  }
}

export interface DkOptions {
  timeoutMs?: number;
  retries?: number;
  chunk?: number;
}

/** The wait for the INFO probe that finds out whether a runtime answers at all. */
export const QUICK_TIMEOUT_MS = 1000;

export class DkClient {
  private dk: Dk;
  private t: DkTransport;
  private seq = 0;
  private timeoutMs: number;
  private retries: number;
  private chunk: number;
  /** Requests take turns: the transport has one reply stream, so two overlapping requests would eat each other's reply. */
  private turn: Promise<unknown> = Promise.resolve();
  constructor(dk: Dk, transport: DkTransport, options: DkOptions = {}) {
    this.dk = dk;
    this.t = transport;
    this.timeoutMs = options.timeoutMs ?? 2000;
    this.retries = options.retries ?? 2;
    this.chunk = options.chunk ?? MAX_CHUNK;
  }
  private takeSeq(): number {
    const s = this.seq;
    this.seq = (this.seq + 1) & 0x7f;
    return s;
  }
  /** One request and its reply, after the requests queued before it; retried with a new seq after a timeout. */
  private request(cmd: number, path = '', num = 0, quick = false): Promise<DkReply> {
    const next = this.turn.then(() => this.exchange(cmd, path, num, quick));
    this.turn = next.catch(() => {});
    return next;
  }
  /** `quick`: one short try (a probe for a device that may not run the runtime). Throws DkError. */
  private async exchange(cmd: number, path: string, num: number, quick: boolean): Promise<DkReply> {
    const retries = quick ? 0 : this.retries;
    const timeoutMs = quick ? Math.min(this.timeoutMs, QUICK_TIMEOUT_MS) : this.timeoutMs;
    for (let attempt = 0; attempt <= retries; attempt++) {
      const seq = this.takeSeq();
      await this.t.send(this.dk.command(cmd, seq, path, num));
      const deadline = Date.now() + timeoutMs;
      for (let left = deadline - Date.now(); left > 0; left = deadline - Date.now()) {
        const raw = await this.t.recv(left);
        if (!raw) break;
        let r: DkReply | null;
        try {
          r = this.dk.decode(raw);
        } catch (e) {
          // A device error carries the seq in the JSON only; match it through a second decode of the header.
          if (raw[6] === seq && (raw[5] & 0x3f) === cmd) throw e;
          continue;
        }
        if (r && r.seq === seq && r.cmd === cmd) return r;
      }
    }
    throw new DkError({ error: 'no reply from the device', kind: 'timeout' });
  }

  /** `quick`: one INFO with a short timeout and no retries. */
  async info(quick = false): Promise<DkInfo> {
    return (await this.request(CMD.INFO, '', 0, quick)).info!;
  }
  async list(): Promise<DkEntry[]> {
    const out: DkEntry[] = [];
    for (;;) {
      const page = (await this.request(CMD.LIST, '', out.length)).list!;
      out.push(...page.entries);
      if (page.entries.length === 0 || out.length >= page.total) return out;
    }
  }
  async delete(path: string): Promise<void> {
    await this.request(CMD.DELETE, path);
  }
  async clear(): Promise<void> {
    await this.request(CMD.CLEAR);
  }
  async abort(): Promise<void> {
    await this.request(CMD.ABORT);
  }

  /** Upload one file. `onProgress` returns false to cancel (the upload is aborted on the device). */
  async put(path: string, data: Uint8Array, onProgress: (p: DkProgress) => boolean | void = () => true): Promise<void> {
    const h = this.dk.putNew(path, data, this.seq, this.chunk);
    let state = this.dk.putState(h);
    try {
      for (;;) {
        for (let m = this.dk.putNext(h); m; m = this.dk.putNext(h)) await this.t.send(m);
        state = this.dk.putState(h);
        if (state.phase === 'done') break;
        const raw = await this.t.recv(this.timeoutMs);
        if (raw) this.dk.putReply(h, raw);
        else this.dk.putTimeout(h);
        state = this.dk.putState(h);
        if (onProgress({ done: state.done, total: state.total }) === false)
          throw new DkError({ error: 'cancelled', kind: 'cancelled' });
      }
      onProgress({ done: state.total, total: state.total });
      this.seq = state.seq;
    } catch (e) {
      this.seq = state.seq;
      // A lost END reply, then E_STATE for the second END: the first one committed. Look the file up.
      if (e instanceof DkError && e.status === 'E_STATE' && state.phase === 'end' && state.end_retried) {
        const found = (await this.list()).some((x) => x.path === path && x.sha256 === state.sha256);
        if (found) return;
      } else if (!(e instanceof DkError && e.kind === 'transport')) {
        await this.abort().catch(() => {});
      }
      throw e;
    } finally {
      this.dk.putFree(h);
    }
  }

  /** CLEAR, then upload `files` again (the device keeps old file data until CLEAR). */
  async repack(
    files: { path: string; data: Uint8Array }[],
    onProgress: (index: number, p: DkProgress) => boolean | void = () => true,
  ): Promise<void> {
    await this.clear();
    for (let i = 0; i < files.length; i++) await this.put(files[i].path, files[i].data, (p) => onProgress(i, p));
  }
}

/** The store entries that no file of the library (path and sha256 in hex) matches: a repack would lose them. */
export function repackMissing(listing: DkEntry[], library: { path: string; sha256: string }[]): string[] {
  return listing.filter((e) => !library.some((l) => l.path === e.path && l.sha256 === e.sha256)).map((e) => e.path);
}

/** The store path of a module file: `modules/<name>`. */
export function storePath(fileName: string): string {
  return `modules/${fileName.replace(/[^\x20-\x7e]/g, '_').replace(/\//g, '_')}`;
}

export async function hexSha256(bytes: Uint8Array): Promise<string> {
  const d = await crypto.subtle.digest('SHA-256', bytes as BufferSource);
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, '0')).join('');
}

// ---- the pure TypeScript client ------------------------------------------------------------------------------------------------

const PREFIX = [0xf0, 0x7d, 0x64, 0x6b];

/** 8-in-7 packing: each group of up to 7 bytes becomes a collector byte (bit `6 - n` is the top bit of byte `n`) and the 7 bytes with bit 7 cleared. */
export function encode8in7(data: Uint8Array): Uint8Array {
  const out = new Uint8Array(Math.ceil(data.length / 7) * 8 - (data.length % 7 ? 7 - (data.length % 7) : 0));
  let at = 0;
  for (let i = 0; i < data.length; i += 7) {
    const n = Math.min(7, data.length - i);
    let top = 0;
    for (let k = 0; k < n; k++) top |= (data[i + k] >> 7) << (6 - k);
    out[at++] = top;
    for (let k = 0; k < n; k++) out[at++] = data[i + k] & 0x7f;
  }
  return out;
}

export function decode8in7(packed: Uint8Array): Uint8Array {
  const out: number[] = [];
  for (let i = 0; i < packed.length; i += 8) {
    const top = packed[i];
    const n = Math.min(7, packed.length - i - 1);
    for (let k = 0; k < n; k++) out.push(packed[i + 1 + k] | (((top >> (6 - k)) & 1) << 7));
  }
  return Uint8Array.from(out);
}

/** One request message, F0 to F7. */
export function frame(
  cmd: number,
  seq: number,
  body: Uint8Array = new Uint8Array(),
  version = PROTO_VERSION,
): Uint8Array {
  const packed = encode8in7(body);
  const out = new Uint8Array(8 + packed.length + 0);
  out.set(PREFIX, 0);
  out[4] = version;
  out[5] = cmd & 0x7f;
  out[6] = seq & 0x7f;
  out.set(packed, 7);
  out[7 + packed.length] = 0xf7;
  return out;
}

export interface RawReply {
  cmd: number;
  seq: number;
  /** 0 is ok; the status codes are `gen_proto.ts` STATUS and LOAD_STATUS. */
  status: number;
  /** The body after the status byte. */
  body: Uint8Array;
}

/** One of our replies, or null for any other message. */
export function parseReply(msg: Uint8Array): RawReply | null {
  if (msg.length < 9 || msg[msg.length - 1] !== 0xf7 || !(msg[5] & 0x40)) return null;
  if (PREFIX.some((b, i) => msg[i] !== b)) return null;
  const body = decode8in7(msg.subarray(7, msg.length - 1));
  if (body.length === 0) return null;
  return { cmd: msg[5] & 0x3f, seq: msg[6], status: body[0] >= 128 ? body[0] - 256 : body[0], body: body.subarray(1) };
}

/** The complete `F0 .. F7` messages at the front of `stream` (bytes before an F0 are dropped); the rest stays in `rest`. */
export function splitMessages(stream: Uint8Array): { messages: Uint8Array[]; rest: Uint8Array } {
  const messages: Uint8Array[] = [];
  let at = 0;
  for (;;) {
    const a = stream.indexOf(0xf0, at);
    if (a < 0) return { messages, rest: new Uint8Array() };
    const b = stream.indexOf(0xf7, a);
    if (b < 0) return { messages, rest: stream.slice(a) };
    messages.push(stream.slice(a, b + 1));
    at = b + 1;
  }
}

const STATUS_NAMES = new Map<number, [string, string]>([
  ...LOAD_STATUS.map(([c, n, t]) => [c, [n, t]] as [number, [string, string]]),
  ...GSTATUS.map(([c, n, t]) => [c, [n, t]] as [number, [string, string]]),
]);

/** `E_NOFILE` for -59 (a protocol status wins over a load status with the same number: E_READ is both). */
export function statusName(code: number): string {
  return STATUS_NAMES.get(code)?.[0] ?? `status ${code}`;
}

/** A device status as an error: `what: E_NOFILE (that path is not in the store)`. */
export function deviceError(what: string, status: number, body?: Uint8Array): DkError {
  const [name, text] = STATUS_NAMES.get(status) ?? [`status ${status}`, ''];
  let free: number | undefined;
  if (name === 'E_FULL' && body && body.length >= 4) free = new DataView(body.buffer, body.byteOffset, 4).getUint32(0);
  return new DkError({
    error: `${what}: ${name}${text ? ` (${text})` : ''}`,
    kind: 'device',
    status: name,
    free_bytes: free,
  });
}

export const fmtVersion = (packed: number): string => `${packed >>> 16}.${(packed >>> 8) & 255}.${packed & 255}`;

/** INFO with the fields of later runtimes (`caps` and the 32-bit runtime version appeared in 0.2). */
export interface DkInfoFull extends DkInfo {
  /** The packed runtime version (major << 16 | minor << 8 | patch). */
  runtime: number;
  caps: number | null;
  card_read_only: boolean;
}

export interface DkModule {
  index: number;
  name: string;
  version: number;
  contracts: [number, number][];
  cf_base: number;
  cf_size: number;
  dsp_off: number;
  dsp_len: number;
  rows: number;
  types: number[];
  faults: number;
  guard_off: boolean;
}

export interface DkLogEvent {
  seq: number;
  time: number;
  kind: number;
  flags: number;
  args: number[];
}

export interface DkSessionOptions {
  timeoutMs?: number;
  retries?: number;
  /** Bytes of a DATA chunk (a multiple of 512, at most 32 KiB). */
  chunk?: number;
}

const u32 = (b: Uint8Array, at: number) => new DataView(b.buffer, b.byteOffset + at, 4).getUint32(0);
const u16 = (b: Uint8Array, at: number) => new DataView(b.buffer, b.byteOffset + at, 2).getUint16(0);
const be32 = (n: number) => Uint8Array.of((n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255);
const text = new TextEncoder();
const cstr = (b: Uint8Array, at: number): [string, number] => {
  const end = b.indexOf(0, at);
  if (end < 0) throw new DkError({ error: 'malformed reply: string without NUL', kind: 'protocol' });
  return [new TextDecoder().decode(b.subarray(at, end)), end + 1];
};
const concat = (...parts: Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
};
const hexOf = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const bytesOfHex = (h: string) => Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16));

/** Does the device have the capability `name` (a key of `CAP`)? From the INFO `caps` word; a runtime without one has none of these. */
export function hasCap(info: DkInfoFull, name: keyof typeof GCAP): boolean {
  return info.caps !== null && ((info.caps >>> GCAP[name]) & 1) === 1;
}

/** The whole protocol over a transport (`send` one message, `recv` the next): pure TypeScript, requests one at a time. */
export class DkSession {
  private t: DkTransport;
  private seq = 0;
  private timeoutMs: number;
  private retries: number;
  private chunk: number;
  private turn: Promise<unknown> = Promise.resolve();
  constructor(transport: DkTransport, options: DkSessionOptions = {}) {
    this.t = transport;
    this.timeoutMs = options.timeoutMs ?? 6000;
    this.retries = options.retries ?? 2;
    this.chunk = options.chunk ?? MAX_CHUNK;
  }

  private takeSeq(): number {
    const s = this.seq;
    this.seq = (this.seq + 1) & 0x7f;
    return s;
  }

  /** One request and its reply (any status), after the requests queued before it. A timeout sends the request again with a new seq. */
  request(cmd: number, body: Uint8Array = new Uint8Array(), quick = false): Promise<RawReply> {
    const next = this.turn.then(() => this.exchange(cmd, body, quick));
    this.turn = next.catch(() => {});
    return next;
  }

  private async exchange(cmd: number, body: Uint8Array, quick: boolean): Promise<RawReply> {
    const retries = quick ? 0 : this.retries;
    const timeoutMs = quick ? Math.min(this.timeoutMs, QUICK_TIMEOUT_MS) : this.timeoutMs;
    for (let attempt = 0; attempt <= retries; attempt++) {
      const seq = this.takeSeq();
      await this.t.send(frame(cmd, seq, body));
      const deadline = Date.now() + timeoutMs;
      for (let left = deadline - Date.now(); left > 0; left = deadline - Date.now()) {
        const raw = await this.t.recv(left);
        if (!raw) break;
        const r = parseReply(raw);
        if (r && r.seq === seq && r.cmd === cmd) return r;
      }
    }
    throw new DkError({ error: 'no reply from the device', kind: 'timeout' });
  }

  private async ok(cmd: number, what: string, body: Uint8Array = new Uint8Array(), quick = false): Promise<Uint8Array> {
    const r = await this.request(cmd, body, quick);
    if (r.status !== 0) throw deviceError(what, r.status, r.body);
    return r.body;
  }

  /** `quick`: one INFO with a short timeout and no retries (a probe for a device that may not run the runtime). */
  async info(quick = false): Promise<DkInfoFull> {
    const b = await this.ok(GCMD.INFO, 'info', undefined, quick);
    const protocol = u16(b, 0);
    let runtime = u16(b, 2);
    const [port, at] = cstr(b, 4);
    const free = u32(b, at);
    const maxChunk = u32(b, at + 4);
    const flags = b[at + 8];
    // appended by later runtimes: the log range (8 bytes), then the packed runtime version and the capability bits
    let caps: number | null = null;
    if (b.length >= at + 9 + 8 + 8) {
      runtime = u32(b, at + 9 + 8);
      caps = u32(b, at + 9 + 12);
    }
    return {
      protocol,
      runtime_version: u16(b, 2),
      runtime,
      caps,
      port,
      free_bytes: free,
      max_chunk: maxChunk,
      safe_mode: (flags & 1) !== 0,
      upload_open: (flags & 2) !== 0,
      card_read_only: (flags & 4) !== 0,
    };
  }

  async list(): Promise<DkEntry[]> {
    const out: DkEntry[] = [];
    for (;;) {
      const b = await this.ok(GCMD.LIST, 'ls', be32(out.length));
      const total = u32(b, 0);
      const n = b[4];
      let at = 5;
      for (let i = 0; i < n; i++) {
        const [path, next] = cstr(b, at);
        out.push({ path, size: u32(b, next), sha256: hexOf(b.subarray(next + 4, next + 36)) });
        at = next + 36;
      }
      if (n === 0 || out.length >= total) return out;
    }
  }

  async delete(path: string): Promise<void> {
    await this.ok(GCMD.DELETE, 'rm', concat(text.encode(path), Uint8Array.of(0)));
  }
  async clear(): Promise<void> {
    await this.ok(GCMD.CLEAR, 'clear', be32(0x434c5221));
  }
  async abort(): Promise<void> {
    await this.ok(GCMD.ABORT, 'abort');
  }

  /** The loaded modules with their ranges, contracts and faults (MODULES, runtime 0.2.0, capability MODULES), every page. */
  async modules(): Promise<DkModule[]> {
    const out: DkModule[] = [];
    let start = 0;
    for (;;) {
      const b = await this.ok(GCMD.MODULES, 'modules', Uint8Array.of(start));
      const total = b[16];
      const n = b[17];
      let at = 18;
      for (let i = 0; i < n; i++) {
        const nt = b[at + 65];
        const name = new TextDecoder().decode(b.subarray(at + 1, at + 33)).split('\0')[0];
        out.push({
          index: b[at],
          name,
          version: u32(b, at + 33),
          contracts: [0, 1, 2, 3].map((k) => [b[at + 37 + 2 * k], b[at + 38 + 2 * k]] as [number, number]),
          cf_base: u32(b, at + 45),
          cf_size: u32(b, at + 49),
          dsp_off: u32(b, at + 53),
          dsp_len: u32(b, at + 57),
          rows: u32(b, at + 61),
          types: Array.from(b.subarray(at + 66, at + 66 + nt)),
          faults: u32(b, at + 66 + nt),
          guard_off: (b[at + 70 + nt] & 1) !== 0,
        });
        start = b[at] + 1;
        at += 71 + nt;
      }
      if (out.length >= total || n === 0) return out;
    }
  }

  /** CAPACITY (runtime 0.2.2): per resource `{limit, used, largest}`, keyed by the kind's name. */
  async capacity(): Promise<Record<string, { limit: number; used: number; largest: number }>> {
    const b = await this.ok(GCMD.CAPACITY, 'capacity');
    const names = new Map(Object.entries(CAPACITY_KINDS).map(([n, k]) => [k as number, n]));
    const out: Record<string, { limit: number; used: number; largest: number }> = {};
    for (let i = 0; i < b[1]; i++) {
      const at = 2 + 13 * i;
      out[names.get(b[at]) ?? `kind${b[at]}`] = {
        limit: u32(b, at + 1),
        used: u32(b, at + 5),
        largest: u32(b, at + 9),
      };
    }
    return out;
  }

  /** READ (runtime 0.2.4): the file size and up to 768 bytes of a store file from `offset` (fewer at the end of the file). */
  async read(path: string, offset: number, length: number): Promise<{ size: number; bytes: Uint8Array }> {
    const b = await this.ok(GCMD.READ, 'read', concat(text.encode(path), Uint8Array.of(0), be32(offset), be32(length)));
    if (b.length < 4) throw new DkError({ error: 'read: malformed reply', kind: 'protocol' });
    return { size: u32(b, 0), bytes: b.subarray(4) };
  }
  /** The first 0xa0 bytes of a store file (a .dkm header). */
  async header(path: string): Promise<Uint8Array> {
    return (await this.read(path, 0, HEADER_BYTES)).bytes;
  }
  /** VERIFY (runtime 0.2.4): the sha256 (hex) of a store file, computed by the device from a fresh read of the card. */
  async verify(path: string): Promise<string> {
    const b = await this.ok(GCMD.VERIFY, 'verify', concat(text.encode(path), Uint8Array.of(0)));
    if (b.length !== 32) throw new DkError({ error: 'verify: malformed reply', kind: 'protocol' });
    return hexOf(b);
  }
  /** SELECT (runtime 0.0.4): replace the selection record (what the next boot loads). */
  async select(paths: string[]): Promise<void> {
    if (paths.length > 8) throw new DkError({ error: 'a selection holds at most 8 paths', kind: 'request' });
    await this.ok(
      GCMD.SELECT,
      'select',
      concat(Uint8Array.of(paths.length), ...paths.map((p) => concat(text.encode(p), Uint8Array.of(0)))),
    );
  }
  /** RESTART: the reply comes first, then the device resets. See `waitForBoot`. */
  async restart(): Promise<void> {
    await this.ok(GCMD.RESTART, 'restart');
  }
  /** LOG: events from `from` on (the log of this boot), `[oldest, newest, events]`. */
  async logPage(from: number, quick = false): Promise<[number, number, DkLogEvent[]]> {
    const b = await this.ok(GCMD.LOG, 'log', be32(from), quick);
    const n = b[8];
    const events: DkLogEvent[] = [];
    for (let i = 0; i < n; i++) {
      const at = 9 + 32 * i;
      events.push({
        seq: u32(b, at),
        time: u32(b, at + 4),
        kind: u16(b, at + 8),
        flags: u16(b, at + 10),
        args: [0, 1, 2, 3, 4].map((k) => u32(b, at + 12 + 4 * k)),
      });
    }
    return [u32(b, 0), u32(b, 4), events];
  }
  /** Every event of the log. */
  async readLog(quick = false): Promise<DkLogEvent[]> {
    const out: DkLogEvent[] = [];
    let from = 0;
    for (;;) {
      const [, newest, events] = await this.logPage(from, quick);
      out.push(...events);
      if (events.length === 0 || events[events.length - 1].seq >= newest) return out;
      from = events[events.length - 1].seq + 1;
    }
  }

  /** Upload one file (BEGIN, DATA chunks one after the other, END). `onProgress(sent, total)` follows each acknowledged chunk. */
  async put(
    path: string,
    data: Uint8Array,
    onProgress: (sent: number, total: number) => void = () => {},
  ): Promise<void> {
    const p = concat(text.encode(path), Uint8Array.of(0));
    const sha = new Uint8Array(await crypto.subtle.digest('SHA-256', data as BufferSource));
    try {
      await this.ok(GCMD.BEGIN, 'begin', concat(p, be32(data.length), sha));
      for (let off = 0; off < data.length; off += this.chunk) {
        const part = data.subarray(off, Math.min(data.length, off + this.chunk));
        // a request that timed out is sent again (the device takes the exact repeat of the last chunk as done)
        await this.ok(GCMD.DATA, `data at ${off}`, concat(be32(off), part));
        onProgress(off + part.length, data.length);
      }
      if (data.length === 0) onProgress(0, 0);
      try {
        await this.ok(GCMD.END, 'end');
      } catch (e) {
        // A lost END reply, then E_STATE for the repeat: the first END committed. Look the file up.
        if (!(e instanceof DkError && e.status === 'E_STATE')) throw e;
        const have = (await this.list()).some((x) => x.path === path && x.sha256 === hexOf(sha));
        if (!have) throw e;
      }
    } catch (e) {
      if (!(e instanceof DkError && (e.kind === 'transport' || e.kind === 'timeout')))
        await this.abort().catch(() => {});
      throw e;
    }
  }

  /**
   * After `restart()`: wait until the device has booted again. A reboot is seen in the log: the old boot's log holds the RESTART request
   * (an UPLOAD event whose first argument is the command), the new boot's log starts with BOOT_START and never saw it. The port may be
   * silent for a while (the emulator boots on a worker): a failed read is a reason to wait and try again.
   */
  async waitForBoot(
    sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
    timeoutMs = 90000,
  ): Promise<void> {
    const end = Date.now() + timeoutMs;
    let last = 'no answer';
    while (Date.now() < end) {
      try {
        const verdict = restarted(await this.readLog(true));
        if (verdict) return;
        last = 'the old boot still runs';
      } catch (e) {
        if (e instanceof DkError && e.kind === 'device' && e.status === 'E_PROTO') throw e;
        last = e instanceof Error ? e.message : String(e);
        if (e instanceof DkError && e.kind === 'refused') throw e;
      }
      await sleep(500);
    }
    throw new DkError({ error: `no new boot within ${Math.round(timeoutMs / 1000)} s (${last})`, kind: 'timeout' });
  }
}

/** The size of a .dkm header (docs/design/digikit-dkm.md). */
export const HEADER_BYTES = 0xa0;
/** The most bytes one READ returns. */
export const READ_MAX = 768;

/**
 * Has the device booted since its RESTART was answered? True: this log has no RESTART request (a new boot, with BOOT_START); false: the
 * old boot still runs or nothing booted. Throws `DkError` kind "refused" when the runtime refused the restart.
 */
export function restarted(events: DkLogEvent[]): boolean {
  let asked = false;
  for (const e of events) {
    if (e.kind === LOG_KIND.UPLOAD && e.args[0] === GCMD.RESTART) {
      if ((e.flags & 1) !== 0 && e.args[1] !== 0)
        throw new DkError({
          error: `the runtime refused the restart (status ${e.args[1] | 0}; a card transfer is in flight, try again)`,
          kind: 'refused',
        });
      asked = true;
    }
  }
  if (asked) return false;
  return events.some((e) => e.kind === LOG_KIND.BOOT_START);
}

export { bytesOfHex, hexOf };
