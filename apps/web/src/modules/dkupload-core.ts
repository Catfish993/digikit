// The module upload protocol (docs/design/digikit-upload.md) in the browser: a thin wrapper over dkupload.wasm
// (packages/dkupload/src/wasm.rs, the same codec and upload state machine the desktop app runs natively) and an
// async driver over a transport. Written without TS-only runtime syntax so node can import it for the tests.

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

export class DkClient {
  private dk: Dk;
  private t: DkTransport;
  private seq = 0;
  private timeoutMs: number;
  private retries: number;
  private chunk: number;
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
  /** One request and its reply; retried with a new seq after a timeout. Throws DkError. */
  private async request(cmd: number, path = '', num = 0): Promise<DkReply> {
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      const seq = this.takeSeq();
      await this.t.send(this.dk.command(cmd, seq, path, num));
      const deadline = Date.now() + this.timeoutMs;
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

  async info(): Promise<DkInfo> {
    return (await this.request(CMD.INFO)).info!;
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
