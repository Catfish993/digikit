// A minimal Chrome DevTools Protocol client for the dev tools that drive the UI preview (ui_shots.mjs, memory_live.mjs): start headless
// Chromium, open a page, evaluate JavaScript in it, take a screenshot. No dependencies.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function chromePath(preferred) {
  const found = [
    preferred,
    process.env.CHROME,
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].find((c) => c && fs.existsSync(c));
  if (!found) throw new Error('no Chrome/Chromium found; pass --chrome PATH');
  return found;
}

export class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (m) => {
      const msg = JSON.parse(m.data);
      const p = msg.id && this.pending.get(msg.id);
      if (p) {
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message));
        else p.resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

/** Open a new tab of the Chromium on `port`: `{ cdp, id }`. */
export async function connect(port) {
  for (let i = 0; i < 100; i++) {
    try {
      const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
      const ws = new WebSocket(target.webSocketDebuggerUrl);
      await new Promise((res, rej) => {
        ws.addEventListener('open', res, { once: true });
        ws.addEventListener('error', rej, { once: true });
      });
      return { cdp: new Cdp(ws), id: target.id };
    } catch {
      await sleep(150);
    }
  }
  throw new Error(`cannot reach Chromium on port ${port}`);
}

/** Close a tab opened by `connect`. */
export async function closeTab(port, { cdp, id }) {
  cdp.ws.close();
  await fetch(`http://127.0.0.1:${port}/json/close/${id}`).catch(() => {});
}

/** Start headless Chromium on `port`: `{ process, stop() }`; `stop` kills only this Chromium and removes its profile. */
export function launchChrome(port, preferred) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-shots-'));
  const proc = spawn(
    chromePath(preferred),
    [
      '--headless=new',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--disable-gpu',
      '--hide-scrollbars=false',
      'about:blank',
    ],
    { stdio: 'ignore' },
  );
  return {
    process: proc,
    async stop() {
      proc.kill();
      await sleep(500);
      try {
        fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
      } catch {
        // Chromium may still be writing its profile in the temp folder; the system cleans it up
      }
    },
  };
}

/** Evaluate `js` in the page (a promise is awaited) and return its value. */
export async function evaluate(cdp, js) {
  const r = await cdp.send('Runtime.evaluate', { expression: js, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
  return r.result.value;
}
