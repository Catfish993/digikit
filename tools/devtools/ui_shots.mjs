#!/usr/bin/env node
// Screenshots of the desktop UI pages in the preview (`mise run ui:preview`), headless Chromium over CDP, no dependencies.
// Each scene sets a state of the fake device through the address, optionally clicks, waits, and writes a PNG.
//
//   node tools/devtools/ui_shots.mjs --out DIR [--prefix before] [--url http://127.0.0.1:5199/] [--scale 1] [scene ...]
//   node tools/devtools/ui_shots.mjs --list
// With no scene names every scene runs. A scene whose step fails still gets its picture (the failure is printed): a missing control is
// itself what a before/after pair shows.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i < 0 ? fallback : args[i + 1];
};
const valueFlags = new Set(['--out', '--prefix', '--url', '--scale', '--chrome', '--port']);
const names = args.filter((a, i) => !a.startsWith('--') && !valueFlags.has(args[i - 1] ?? ''));
const base = opt('url', 'http://127.0.0.1:5199/');
const out = opt('out', 'ui-shots');
const prefix = opt('prefix', '');
const scale = Number(opt('scale', 1));
const port = Number(opt('port', 9611));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- the scenes ---------------------------------------------------------------------------------------------------------------------
const WIDE = [1440, 900];
const MIN = [700, 600];
/** Page helpers: `p.eval(js)`, `p.click(text)`, `p.tick(labelRegexSource)`, `p.hover(selector)`, `p.wait(ms)`. */
const SCENES = [
  { name: 'modules', q: 'mode=modules', size: WIDE },
  {
    name: 'modules-select-partial',
    q: 'mode=modules',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Install (Sine|Pluck)$');
      await p.tick('^Delete (Sine)$');
    },
  },
  {
    name: 'modules-uploading',
    q: 'mode=modules&speed=0.4',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Install (Wavetable|FM|Pluck)');
      await p.click(/^Install/);
      await p.wait(2500);
    },
  },
  { name: 'modules-narrow', q: 'mode=modules', size: MIN },
  {
    name: 'modules-narrow-uploading',
    q: 'mode=modules&speed=0.4',
    size: MIN,
    steps: async (p) => {
      await p.tick('^Install (Wavetable|FM|Pluck)');
      await p.click(/^Install/);
      await p.wait(2500);
    },
  },
  { name: 'samples', q: 'mode=samples', size: WIDE },
  {
    name: 'samples-select-partial',
    q: 'mode=samples',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Upload (kick|snare)');
      await p.tick('^Delete (Drums|init_tone)$');
    },
  },
  {
    name: 'samples-uploading',
    q: 'mode=samples&speed=1.2',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Upload (kick|snare|hat)');
      await p.click(/^Upload/);
      await p.wait(3000);
    },
  },
  { name: 'samples-narrow', q: 'mode=samples', size: MIN },
  {
    name: 'samples-narrow-uploading',
    q: 'mode=samples&speed=1.2',
    size: MIN,
    steps: async (p) => {
      await p.tick('^Upload (kick|snare|hat)');
      await p.click(/^Upload/);
      await p.wait(3000);
    },
  },
  { name: 'samples-no-folder', q: 'mode=samples&folder=none', size: WIDE },
  {
    name: 'noreply',
    q: 'mode=modules&dev=noreply',
    size: WIDE,
    steps: async (p) => {
      await p.wait(2600);
    },
  },
  {
    name: 'noreply-samples',
    q: 'mode=samples&dev=noreply',
    size: WIDE,
    steps: async (p) => {
      await p.wait(2600);
    },
  },
  {
    name: 'upload-not-waiting',
    q: 'mode=samples&speed=0.3',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Upload (perc_shaker|pad_warm)');
      await p.click(/^Upload/);
      await p.wait(4500);
    },
  },
  {
    name: 'samples-scrolled-narrow',
    q: 'mode=samples',
    size: MIN,
    steps: async (p) => {
      // the left pane scrolls alone: the right pane and the divider stay, the header sticks to the top of its pane
      await p.eval("document.querySelector('.split > section').scrollTop = 300");
      await p.wait(300);
      const tops = await p.eval('[...document.querySelectorAll(".split > section")].map((e) => e.scrollTop)');
      const handle = await p.eval(
        '(() => { const h = document.querySelector(".split-handle").getBoundingClientRect(); const s = document.querySelector(".split").getBoundingClientRect(); return [h.top - s.top, s.bottom - h.bottom]; })()',
      );
      p.say(
        `pane scrollTop ${JSON.stringify(tops)}; divider insets from the split box top/bottom ${JSON.stringify(handle)}`,
      );
    },
  },
  {
    name: 'auto-refresh',
    q: 'mode=samples&speed=0.3',
    size: WIDE,
    steps: async (p) => {
      const counts = () => p.eval('JSON.stringify(window.__sim.counts)');
      const has = (name) => p.eval(`document.body.innerText.includes(${JSON.stringify(name)})`);
      await p.wait(1500);
      const c0 = JSON.parse(await counts());
      await p.wait(7000);
      const c1 = JSON.parse(await counts());
      p.say(
        `idle 7 s: local polls +${c1.localPoll - c0.localPoll} (expect 2), device reads +${c1.deviceRead - c0.deviceRead} (expect 0)`,
      );
      await p.eval("window.__sim.addLocal('new_file_xyz.wav')");
      await p.wait(4500);
      p.say(`file added on disk: shown after the poll = ${await has('new_file_xyz.wav')} (expect true)`);
      const c2 = JSON.parse(await counts());
      await p.eval("window.dispatchEvent(new Event('focus'))");
      await p.wait(1500);
      const c3 = JSON.parse(await counts());
      p.say(
        `focus: device reads +${c3.deviceRead - c2.deviceRead} (expect 1), local reads +${c3.localList - c2.localList} (expect 1)`,
      );
      await p.eval("window.dispatchEvent(new Event('focus'))");
      await p.eval("document.dispatchEvent(new Event('visibilitychange'))");
      await p.wait(1200);
      const c4 = JSON.parse(await counts());
      p.say(
        `focus + visibilitychange together: device reads +${c4.deviceRead - c3.deviceRead} (expect 1 or 0: one read answers both)`,
      );
      // a job: nothing reads while it runs, and the file that appears meanwhile shows after the job
      await p.tick('^Upload (perc_shaker|pad_warm)');
      await p.click(/^Upload/);
      await p.wait(800);
      const c5 = JSON.parse(await counts());
      await p.eval("window.__sim.addLocal('during_upload.wav')");
      await p.eval("window.dispatchEvent(new Event('focus'))");
      await p.wait(6000);
      const c6 = JSON.parse(await counts());
      p.say(
        `during the upload (6 s): device reads +${c6.deviceRead - c5.deviceRead}, local polls +${c6.localPoll - c5.localPoll}, local reads +${c6.localList - c5.localList} (expect 0, 0, 0); new file shown yet = ${await has('during_upload.wav')} (expect false)`,
      );
    },
  },
  {
    name: 'samples-done',
    q: 'mode=samples&speed=8',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Upload (kick_punchy|hat_closed_a)');
      await p.click(/^Upload/);
      await p.wait(4000);
    },
  },
  {
    name: 'delete-confirm',
    q: 'mode=modules',
    size: WIDE,
    steps: async (p) => {
      await p.tick('^Delete (Pluck|Chorus)$');
      await p.click(/^Delete/);
      await p.wait(300);
    },
  },
  { name: 'slow-not-waiting', q: 'mode=modules&dev=slow', size: WIDE, steps: async (p) => p.wait(0) },
  { name: 'empty', q: 'mode=modules&dev=empty', size: WIDE },
  { name: 'empty-samples', q: 'mode=samples&dev=empty', size: WIDE },
  { name: 'error', q: 'mode=modules&dev=error', size: WIDE },
];

if (args.includes('--list')) {
  console.log(SCENES.map((s) => s.name).join('\n'));
  process.exit(0);
}

// ---- a minimal CDP client ----------------------------------------------------------------------------------------------------------
function chromePath() {
  const found = [
    opt('chrome'),
    process.env.CHROME,
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].find((c) => c && fs.existsSync(c));
  if (!found) throw new Error('no Chrome/Chromium found; pass --chrome PATH');
  return found;
}

class Cdp {
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

async function connect(url) {
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

function pageApi(cdp) {
  const evaluate = async (js) => {
    const r = await cdp.send('Runtime.evaluate', { expression: js, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  };
  const api = {
    eval: evaluate,
    say: (m) => console.log(`  ${m}`),
    wait: sleep,
    async click(text) {
      const src = text instanceof RegExp ? text.source : `^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`;
      const ok = await evaluate(
        `(() => { const re = new RegExp(${JSON.stringify(src)}); const b = [...document.querySelectorAll('button')].find((x) => re.test(x.textContent.trim()) && !x.disabled); if (b) b.click(); return !!b; })()`,
      );
      if (!ok) throw new Error(`no enabled button ${src}`);
    },
    async tick(labelSrc) {
      const n = await evaluate(
        `(() => { const re = new RegExp(${JSON.stringify(labelSrc)}); const xs = [...document.querySelectorAll('input[type=checkbox]')].filter((x) => re.test(x.getAttribute('aria-label') ?? '') && !x.checked && !x.disabled); xs.forEach((x) => x.click()); return xs.length; })()`,
      );
      if (!n) throw new Error(`no checkbox ${labelSrc}`);
    },
    async hover(selector) {
      const box = await evaluate(
        `(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()`,
      );
      if (!box) throw new Error(`nothing matches ${selector}`);
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box[0], y: box[1] });
    },
  };
  return api;
}

// ---- run -----------------------------------------------------------------------------------------------------------------------------
fs.mkdirSync(out, { recursive: true });
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-shots-'));
const chrome = spawn(
  chromePath(),
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
let failed = 0;
try {
  const chosen = names.length ? SCENES.filter((s) => names.includes(s.name)) : SCENES;
  for (const scene of chosen) {
    const { cdp, id } = await connect();
    const p = pageApi(cdp);
    await cdp.send('Page.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: scene.size[0],
      height: scene.size[1],
      deviceScaleFactor: scale,
      mobile: false,
    });
    await cdp.send('Page.navigate', { url: `${base}?${scene.q}` });
    await sleep(1500);
    let note = '';
    try {
      await scene.steps?.(p);
      if (!scene.steps) await sleep(800);
    } catch (e) {
      note = ` (step failed: ${e.message})`;
      failed++;
    }
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(out, `${prefix ? `${prefix}-` : ''}${scene.name}.png`);
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    console.log(`${file}${note}`);
    cdp.ws.close();
    await fetch(`http://127.0.0.1:${port}/json/close/${id}`).catch(() => {});
  }
} finally {
  chrome.kill();
  await sleep(500);
  try {
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  } catch {
    // Chromium may still be writing its profile in the temp folder; the system cleans it up
  }
}
process.exit(0);
