#!/usr/bin/env node
// Headless-browser throughput bench of the coupled browser core (page mode
// `/?bench=1&seconds=N`, packages/web/src/runtime/bench.ts). Drives Chrome or
// Chromium over CDP with Node's built-in fetch/WebSocket (no dependencies),
// starts the astro dev server itself unless --url is given, and prints one
// JSON document.
//
//   mise run web:bench -- --seconds 10 --runs 3
//   mise run web:bench -- --js-flags=--no-liftoff
//   mise run web:bench -- --profile            # top wasm functions by self time
//
// Options: --seconds N (10) --runs N (1) --js-flags "..." --profile
//   --url http://host:port (use a running dev server) --port N (4399)
//   --chrome PATH (else $CHROME, Google Chrome, Chromium, Edge)
//   --syx PATH (else $DIGI_EMU_SYX, repo-root Digitone_II_OS1.11.syx, main tree)
//   --timeout S (900) --headed
// The dev server serves the local DSP inputs and the coupled wasm only to
// loopback; nothing firmware-derived is written by this tool. Timed runs must
// hold the bench lock: `mise run web:bench` does (exclusive).
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.findIndex((a) => a === `--${name}` || a.startsWith(`--${name}=`));
  if (i < 0) return fallback;
  return args[i].includes('=') ? args[i].slice(args[i].indexOf('=') + 1) : args[i + 1];
};
const flag = (name) => args.includes(`--${name}`);
const seconds = Number(opt('seconds', 10));
const runs = Number(opt('runs', 1));
const jsFlags = opt('js-flags', '');
const profile = flag('profile');
const timeoutS = Number(opt('timeout', 900));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...m) => console.error('[browser_bench]', ...m);

function findChrome() {
  const candidates = [opt('chrome'), process.env.CHROME, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'];
  const found = candidates.find((c) => c && fs.existsSync(c));
  if (!found) throw new Error('no Chrome/Chromium found; pass --chrome PATH');
  return found;
}

function findSyx() {
  const name = 'Digitone_II_OS1.11.syx';
  const main = root.includes(`${path.sep}.claude${path.sep}worktrees${path.sep}`) ? root.split(`${path.sep}.claude${path.sep}`)[0] : root;
  const found = [opt('syx'), process.env.DIGI_EMU_SYX, path.join(root, name), path.join(main, name)].find((c) => c && fs.existsSync(c));
  if (!found) throw new Error(`firmware ${name} not found; pass --syx PATH`);
  return found;
}

async function waitHttp(url, ms = 120000) {
  const end = Date.now() + ms;
  for (;;) {
    try { if ((await fetch(url)).ok) return; } catch { /* not up yet */ }
    if (Date.now() > end) throw new Error(`timeout waiting for ${url}`);
    await sleep(300);
  }
}

function startDevServer(port, syx) {
  const cwd = path.join(root, 'packages', 'web');
  const bin = path.join(cwd, 'node_modules', '.bin', 'astro');
  const child = spawn(bin, ['dev', '--host', '127.0.0.1', '--port', String(port)], { cwd, env: { ...process.env, DIGI_EMU_SYX: syx, ...(fs.existsSync(path.join(root, 'node_modules')) ? { DIGI_VITE_FS_ALLOW: path.dirname(fs.realpathSync(path.join(root, 'node_modules'))) } : {}) }, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', () => {}); child.stderr.on('data', () => {});
  return child;
}

class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; ws.onmessage = (e) => this.onMessage(JSON.parse(e.data)); }
  static async connect(url) { const ws = new WebSocket(url); await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; }); return new Cdp(ws); }
  onMessage(m) {
    if (m.id && this.pending.has(m.id)) { const { resolve, reject } = this.pending.get(m.id); this.pending.delete(m.id); if (m.error) reject(new Error(`${m.error.message}`)); else resolve(m.result); return; }
    if (m.method) for (const h of this.handlers) h(m);
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })); });
  }
  close() { this.ws.close(); }
}

// Self time per wasm/JS function from a CPU profile; DSP kernels are the
// generated `sharc` functions, the rest is ColdFire, host glue and JS.
function selfTimes(profileData) {
  const byId = new Map(profileData.nodes.map((n) => [n.id, n]));
  const self = new Map();
  const deltas = profileData.timeDeltas;
  profileData.samples.forEach((id, i) => {
    const node = byId.get(id); const cf = node.callFrame;
    const key = `${cf.functionName || '(anon)'} ${cf.url ? cf.url.split('/').pop() : ''}`.trim();
    self.set(key, (self.get(key) ?? 0) + (deltas[i] ?? 0));
  });
  const total = [...self.values()].reduce((a, b) => a + b, 0);
  // DSP: generated SHARC+ kernels and the engine around them. ColdFire: interpreter, bus and board. Idle is the worker waiting.
  const categories = { dsp: 0, coldfire: 0, idle: 0, other: 0 };
  for (const [name, us] of self) {
    const kind = /^\(idle\)/.test(name) ? 'idle' : /sharc_gen|sharc_native|sharc_rt|sharc_fast/.test(name) ? 'dsp' : /coldfire|elektron_native|machine|periph|plusdrive|card|loader/.test(name) ? 'coldfire' : 'other';
    categories[kind] += us;
  }
  const busy = categories.dsp + categories.coldfire + categories.other;
  const split = { ...Object.fromEntries(Object.entries(categories).map(([k, us]) => [`${k}_ms`, +(us / 1000).toFixed(1)])), dsp_share_of_busy: +(categories.dsp / busy).toFixed(3), coldfire_share_of_busy: +(categories.coldfire / busy).toFixed(3) };
  const top = [...self.entries()].sort((a, b) => b[1] - a[1]).slice(0, 25).map(([name, us]) => ({ name, self_ms: +(us / 1000).toFixed(1), pct: +(100 * us / total).toFixed(2) }));
  return { total_ms: +(total / 1000).toFixed(1), split, top, all: self };
}

async function oneRun(url, chrome, port) {
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-bench-'));
  const chromeArgs = ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`, '--no-first-run', '--no-default-browser-check',
    '--autoplay-policy=no-user-gesture-required', '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
    ...(jsFlags ? [`--js-flags=${jsFlags}`] : []), 'about:blank'];
  if (flag('headed')) chromeArgs.splice(0, 1);
  const proc = spawn(chrome, chromeArgs, { stdio: ['ignore', 'ignore', 'ignore'] });
  try {
    await waitHttp(`http://127.0.0.1:${port}/json/version`, 30000);
    const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    const cdp = await Cdp.connect(version.webSocketDebuggerUrl);
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    const workerSessions = [];
    const profiles = [];
    cdp.handlers.push((m) => {
      if (m.sessionId && m.method === 'Runtime.exceptionThrown') log('page exception:', m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text);
      if (m.sessionId && m.method === 'Runtime.consoleAPICalled' && flag('verbose')) log('console:', m.params.args.map((a) => a.value ?? a.description).join(' '));
      if (m.method === 'Target.detachedFromTarget') { const k = workerSessions.indexOf(m.params.sessionId); if (k >= 0) workerSessions.splice(k, 1); }
      if (m.method === 'Target.attachedToTarget' && m.params.targetInfo.type === 'worker') {
        const ws = m.params.sessionId; workerSessions.push(ws);
        (async () => {
          await cdp.send('Runtime.enable', {}, ws).catch(() => {});
          if (profile) { await cdp.send('Profiler.enable', {}, ws); await cdp.send('Profiler.setSamplingInterval', { interval: 500 }, ws); await cdp.send('Profiler.start', {}, ws); }
          await cdp.send('Runtime.runIfWaitingForDebugger', {}, ws);
        })().catch((e) => log('worker attach failed:', e.message));
      }
    });
    await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    await cdp.send('Page.enable', {}, sessionId);
    // Warm the dev server (Vite may optimise dependencies and reload on the first visit).
    await cdp.send('Page.navigate', { url: `${url}/` }, sessionId);
    await sleep(4000);
    await cdp.send('Page.navigate', { url: `${url}/?bench=1&seconds=${seconds}${profile ? '&hold=1' : ''}` }, sessionId);
    const end = Date.now() + timeoutS * 1000;
    let result;
    for (;;) {
      const r = await cdp.send('Runtime.evaluate', { expression: 'JSON.stringify(window.__bench ?? null)', returnByValue: true }, sessionId).catch(() => undefined);
      const v = r?.result?.value ? JSON.parse(r.result.value) : null;
      if (v && v.state !== 'running') { result = v; break; }
      if (Date.now() > end) throw new Error('bench timed out');
      await sleep(500);
    }
    if (profile) {
      for (const ws of workerSessions) {
        const { profile: p } = await cdp.send('Profiler.stop', {}, ws).catch((e) => { log('Profiler.stop failed:', e.message); return {}; });
        if (p) profiles.push(selfTimes(p));
      }
      if (profiles.length) {
        const worst = profiles.sort((a, b) => b.total_ms - a.total_ms)[0];
        result.profile = { total_ms: worst.total_ms, split: worst.split, top: worst.top.map((t) => ({ ...t, name: t.name.replace(/^_R[A-Za-z]*?\d+/, '').replace(/ emulator-core-sharc.wasm$/, '').slice(0, 110) })) };
      }
    }
    cdp.close();
    return result;
  } finally {
    proc.kill('SIGKILL');
    await sleep(300);
    fs.rmSync(profileDir, { recursive: true, force: true });
  }
}

const chrome = findChrome();
let server;
let url = opt('url');
try {
  if (!url) {
    const port = Number(opt('port', 4399));
    const syx = findSyx();
    log(`starting astro dev on ${port} (firmware ${syx})`);
    server = startDevServer(port, syx);
    url = `http://127.0.0.1:${port}`;
    await waitHttp(url);
  }
  const out = [];
  for (let i = 0; i < runs; i += 1) {
    log(`run ${i + 1}/${runs} flags=${JSON.stringify(jsFlags)} seconds=${seconds}`);
    const r = await oneRun(url, chrome, 9400 + i);
    out.push(r);
    log(r.state === 'done' ? `audio/wall ${r.audio_per_wall} (first half ${r.rate_first_half}, last half ${r.rate_last_half})` : `error: ${r.error}`);
  }
  const summary = { chrome, js_flags: jsFlags, seconds, profile, runs: out };
  console.log(JSON.stringify(summary, null, 2));
  if (out.some((r) => r.state !== 'done')) process.exitCode = 1;
} finally {
  if (server) server.kill('SIGTERM');
}
