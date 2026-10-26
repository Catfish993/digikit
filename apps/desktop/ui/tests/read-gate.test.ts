type Timer = { fn: () => void; at: number; live: boolean };

// When a panel reads (apps/desktop/ui/src/read-gate-core.ts): one read at a time, the triggers that arrive meanwhile make ONE more, a failed
// read is tried again with a growing gap a few times, and a hidden panel reads nothing.
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { createReadGate, RETRY_MS } from '../src/read-gate-core.ts';

/** A clock the test moves, and a read the test finishes. */
function rig() {
  const timers: Timer[] = [];
  let now = 0;
  const reads: { finish: (ok: boolean) => void; current: () => boolean; startedAt: number }[] = [];
  const gate = createReadGate({
    run: (current) => new Promise((resolve) => reads.push({ finish: resolve, current, startedAt: now })),
    setTimer: (fn, ms) => {
      const t = { fn, at: now + ms, live: true };
      timers.push(t);
      return t;
    },
    clearTimer: (t) => ((t as Timer).live = false),
  });
  const tick = async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  };
  return {
    gate,
    reads,
    timers: () => timers.filter((t) => t.live),
    async finish(i: number, ok: boolean) {
      reads[i].finish(ok);
      await tick();
    },
    async advance(ms: number) {
      now += ms;
      for (const t of timers.filter((x) => x.live && x.at <= now)) {
        t.live = false;
        t.fn();
      }
      await tick();
    },
  };
}

test('a second read does not start while one is in flight; any number of triggers make one more', async () => {
  const r = rig();
  r.gate.request();
  assert.equal(r.reads.length, 1);
  for (let i = 0; i < 10; i++) r.gate.request(); // focus, job end, restart, Refresh ...
  assert.equal(r.reads.length, 1, 'still one read');
  await r.finish(0, true);
  assert.equal(r.reads.length, 2, 'the kept triggers made exactly one more');
  await r.finish(1, true);
  assert.equal(r.reads.length, 2, 'and nothing after it');
});

test('a failed read is tried again after growing gaps, three times, then waits for the next trigger', async () => {
  const r = rig();
  r.gate.request();
  await r.finish(0, false);
  assert.deepEqual(RETRY_MS, [3000, 9000, 27000]);
  await r.advance(2999);
  assert.equal(r.reads.length, 1);
  await r.advance(1);
  assert.equal(r.reads.length, 2, 'retry 1 after 3 s');
  await r.finish(1, false);
  await r.advance(8999);
  assert.equal(r.reads.length, 2);
  await r.advance(1);
  assert.equal(r.reads.length, 3, 'retry 2 after 9 s more');
  await r.finish(2, false);
  await r.advance(27000);
  assert.equal(r.reads.length, 4, 'retry 3 after 27 s more');
  await r.finish(3, false);
  await r.advance(10 * 60_000);
  assert.equal(r.reads.length, 4, 'no fourth retry: a silent device is not polled');
  assert.equal(r.timers().length, 0);
  r.gate.request();
  assert.equal(r.reads.length, 5, 'a trigger reads again');
});

test('triggers during a failing read do not turn into an immediate retry', async () => {
  const r = rig();
  r.gate.request();
  r.gate.request();
  await r.finish(0, false);
  assert.equal(r.reads.length, 1, 'the retry waits its 3 s');
  assert.equal(r.timers().length, 1);
});

test('a read that throws counts as failed: it is tried again after the first gap', async () => {
  const timers: number[] = [];
  let runs = 0;
  const gate = createReadGate({
    run: async () => {
      runs++;
      throw new Error('no reply');
    },
    setTimer: (fn, ms) => timers.push(ms),
    clearTimer: () => {},
  });
  gate.request();
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(runs, 1);
  assert.deepEqual(timers, [3000]);
});

test('a success after a retry starts the gaps over', async () => {
  const r = rig();
  r.gate.request();
  await r.finish(0, false);
  await r.advance(3000);
  await r.finish(1, true);
  r.gate.request();
  await r.finish(2, false);
  assert.equal(r.timers().length, 1);
  await r.advance(3000);
  assert.equal(r.reads.length, 4, 'the first gap again, 3 s');
});

test('stop: a hidden or collapsed panel drops its retry and the read that was kept; a read in flight ends unheard', async () => {
  const r = rig();
  r.gate.request();
  await r.finish(0, false);
  assert.equal(r.timers().length, 1);
  r.gate.stop();
  assert.equal(r.timers().length, 0, 'the retry is gone');
  await r.advance(60_000);
  assert.equal(r.reads.length, 1, 'nothing reads while hidden');

  r.gate.request();
  assert.equal(r.reads.length, 2);
  r.gate.request(); // kept
  assert.equal(r.reads[1].current(), true);
  r.gate.stop();
  assert.equal(r.reads[1].current(), false, 'the answer of a read that began before the stop is not wanted');
  await r.finish(1, true);
  assert.equal(r.reads.length, 2, 'the kept trigger was dropped with the stop');
});

test('shown again while the read from before the stop still runs: one read follows it', async () => {
  const r = rig();
  r.gate.request();
  r.gate.stop();
  r.gate.request(); // shown again; the first read still runs
  assert.equal(r.reads.length, 1);
  await r.finish(0, true);
  assert.equal(r.reads.length, 2);
  assert.equal(r.reads[1].current(), true);
});

test('dispose: nothing starts and the running read is no longer current', async () => {
  const r = rig();
  r.gate.request();
  r.gate.dispose();
  assert.equal(r.reads[0].current(), false);
  await r.finish(0, false);
  r.gate.request();
  assert.equal(r.reads.length, 1);
  assert.equal(r.timers().length, 0);
});
