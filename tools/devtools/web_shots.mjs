#!/usr/bin/env node
// Headless Chrome walk through the patch gallery and the hand-off to the emulator page, over CDP (no
// dependencies, like tools/devtools/browser_bench.mjs). Saves PNGs and checks the downloaded bytes.
//
//   node tools/devtools/web_shots.mjs --url http://127.0.0.1:4331 --firmware firmware/Digitakt_II_OS1.17.syx --module fx/master-gain
//        [--out out/web-shots] [--expect PATCHED.syx] [--no-boot] [--chrome PATH] [--port 9450]
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (n, d) => {
  const i = args.indexOf(`--${n}`);
  return i >= 0 ? args[i + 1] : d;
};
const url = opt('url', 'http://127.0.0.1:4331').replace(/\/$/, '');
const fw = path.resolve(opt('firmware'));
const moduleId = opt('module', 'fx/master-gain');
const out = path.resolve(opt('out', 'out/web-shots'));
const port = Number(opt('port', 9450));
const chrome = [
  opt('chrome'),
  process.env.CHROME,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
].find((p) => p && existsSync(p));
if (!chrome) throw new Error('no Chrome found; pass --chrome PATH');
mkdirSync(out, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const proc = spawn(
  chrome,
  [
    '--headless=new',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${mkdtempSync(path.join(tmpdir(), 'shots-'))}`,
    '--no-first-run',
    '--hide-scrollbars',
    'about:blank',
  ],
  { stdio: 'ignore' },
);
let ws;
for (let i = 0; i < 100 && !ws; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    const page = list.find((t) => t.type === 'page');
    if (page) {
      ws = new WebSocket(page.webSocketDebuggerUrl);
      await new Promise((res, rej) => {
        ws.onopen = res;
        ws.onerror = rej;
      });
    }
  } catch {
    ws = undefined;
    await sleep(200);
  }
}
let id = 0;
const waiting = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) {
    const w = waiting.get(m.id);
    waiting.delete(m.id);
    if (m.error) w.rej(new Error(m.error.message));
    else w.res(m.result);
  }
};
const cdp = (method, params = {}) =>
  new Promise((res, rej) => {
    const i = ++id;
    waiting.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params }));
  });
const ev = async (expression) => {
  const r = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'eval failed');
  return r.result.value;
};
const until = async (expression, what, ms = 60000) => {
  const t = Date.now();
  while (Date.now() - t < ms) {
    if (await ev(expression).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`timeout waiting for ${what}`);
};
const shot = async (name, width = 1280, height = 900, scheme = 'light') => {
  await cdp('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 600 });
  await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }] });
  await sleep(300);
  const { data } = await cdp('Page.captureScreenshot', { format: 'png' });
  writeFileSync(path.join(out, `${name}.png`), Buffer.from(data, 'base64'));
  console.log(`shot ${name}.png`);
};
const go = async (u) => {
  await cdp('Page.navigate', { url: u });
  await sleep(500);
};
await cdp('Page.enable');
await cdp('DOM.enable');
await cdp('Runtime.enable');
const failures = [];
const expect = (ok, what) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`);
  if (!ok) failures.push(what);
};

try {
  await go(`${url}/patch/`);
  await until(`document.querySelectorAll('.card').length > 0`, 'module cards');
  await shot('patch-empty');
  // choose the firmware
  const { root } = await cdp('DOM.getDocument');
  const { nodeId } = await cdp('DOM.querySelector', { nodeId: root.nodeId, selector: '#firmware-file' });
  await cdp('DOM.setFileInputFiles', { nodeId, files: [fw] });
  await until(`document.querySelector('.loaded-name .badge.ok')`, 'firmware identified');
  expect(true, `identified: ${await ev(`document.querySelector('.loaded-name').textContent`)}`);
  const card = `[...document.querySelectorAll('.card')].find((c) => c.textContent.includes('${moduleId}'))`;
  expect(await ev(`${card}?.textContent.includes('Works with')`), 'compatibility note');
  await ev(`${card}.querySelector('button.add').click()`);
  await shot('patch-selected');
  await ev(`document.querySelector('.patchbar .btn.primary').click()`);
  await until(`document.querySelector('.dialog a[download]')`, 'result dialog', 120000);
  const dl = await ev(
    `(async () => { const a = document.querySelector('.dialog a[download]'); const b = new Uint8Array(await (await fetch(a.href)).arrayBuffer()); const h = await crypto.subtle.digest('SHA-256', b); return { name: a.download, len: b.length, sha: [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join('') }; })()`,
  );
  console.log('download', JSON.stringify(dl));
  expect(/^(Digitakt_II|Digitone_II)_OS[\d.]+\+.+\.syx$/.test(dl.name), `file name ${dl.name}`);
  const want = opt('expect');
  if (want)
    expect(
      createHash('sha256').update(readFileSync(want)).digest('hex') === dl.sha,
      'download equals the command-line output',
    );
  await ev(`document.querySelector('.dialog').scrollIntoView()`);
  await shot('patch-result');
  await shot('patch-result-dark', 1280, 900, 'dark');
  await shot('patch-phone', 390, 844, 'light');
  if (!args.includes('--no-boot')) {
    await ev(
      `[...document.querySelectorAll('.dialog button')].find((b) => b.textContent.includes('Run in emulator')).click()`,
    );
    await until(`location.search.includes('handoff=1')`, 'navigation to the emulator', 20000);
    await until(
      `document.querySelector('main.emulator')?.dataset.loaded === 'true' && document.querySelector('.runbar strong')?.textContent.includes('OS')`,
      'emulator running the patched firmware',
      180000,
    );
    expect(true, `emulator: ${await ev(`document.querySelector('.runbar').textContent`)}`);
    await sleep(3000);
    await shot('emulator-patched', 1280, 900, 'light');
    await shot('emulator-phone', 390, 844, 'dark');
  }
} finally {
  ws.close();
  proc.kill();
}
if (failures.length) {
  console.error(`failed: ${failures.join('; ')}`);
  process.exit(1);
}
