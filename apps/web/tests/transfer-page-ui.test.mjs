// The pure logic behind the Transfer page's behaviour: select all, the speed and the time left, when the page reads by itself, when it says
// "Waiting for ...", the bottom bar of both modes (apps/desktop/ui/src: select-core, rate-core, refresh-core, wait-core, activity, transfer-core,
// samples-core). The pictures of the page are checked with `node tools/devtools/ui_shots.mjs` (docs/TESTING.md).
import assert from 'node:assert/strict';
import test from 'node:test';
import { instrument } from '../../desktop/ui/src/activity.ts';
import { createRate, etaSeconds, fmtEta, fmtRate, statsLine } from '../../desktop/ui/src/rate-core.ts';
import { FOCUS_GAP_MS, canAutoRefresh, focusReadDue, listingChanged } from '../../desktop/ui/src/refresh-core.ts';
import { applyUpload, samplesBar } from '../../desktop/ui/src/samples-core.ts';
import { selectState, toggleAll } from '../../desktop/ui/src/select-core.ts';
import { countLabel, applyEvent, modulesBar, startProgress } from '../../desktop/ui/src/transfer-core.ts';
import { WAIT_QUIET_MS, createActivity, waitShown } from '../../desktop/ui/src/wait-core.ts';

test('select all: none, some or all of the visible rows; a click ticks all, or clears them when all are ticked', () => {
  const keys = ['a', 'b', 'c'];
  assert.equal(selectState(keys, new Set()), 'none');
  assert.equal(selectState(keys, new Set(['b'])), 'some');
  assert.equal(selectState(keys, new Set(['a', 'b', 'c'])), 'all');
  assert.equal(selectState([], new Set(['x'])), 'none', 'no rows: nothing to select');
  // ticks of rows that are not visible do not count
  assert.equal(selectState(keys, new Set(['x', 'y'])), 'none');
  assert.equal(selectState(keys, new Set(['a', 'b', 'c', 'x'])), 'all');
  // a click: none and some tick everything visible; all clears the visible rows
  assert.deepEqual([...toggleAll(keys, new Set())].sort(), ['a', 'b', 'c']);
  assert.deepEqual([...toggleAll(keys, new Set(['b']))].sort(), ['a', 'b', 'c']);
  assert.deepEqual([...toggleAll(keys, new Set(['a', 'b', 'c']))], []);
  // rows of another folder (not visible) keep their ticks either way
  assert.deepEqual([...toggleAll(keys, new Set(['x']))].sort(), ['a', 'b', 'c', 'x']);
  assert.deepEqual([...toggleAll(keys, new Set(['a', 'b', 'c', 'x']))], ['x']);
  const ticked = new Set(['a']);
  toggleAll(keys, ticked);
  assert.deepEqual([...ticked], ['a'], 'the given set is not changed');
});

test('button counts: in parentheses, none when nothing is ticked', () => {
  assert.equal(countLabel('Upload', 0), 'Upload');
  assert.equal(countLabel('Upload', 1), 'Upload (1)');
  assert.equal(countLabel('Install', 12), 'Install (12)');
});

test('the rate is smoothed over the last 2 s and falls when the transfer stops', () => {
  const r = createRate();
  assert.equal(r.rate(0), undefined, 'nothing yet');
  r.add(0, 0);
  assert.equal(r.rate(100), undefined, 'one sample, or too short a span, says nothing');
  r.add(1000, 1_000_000);
  assert.equal(r.rate(1000), 1_000_000);
  // a burst at the end does not jump the number: it is the average over the window
  r.add(1100, 2_000_000);
  const mid = r.rate(1100);
  assert.ok(mid > 1_000_000 && mid < 2_000_000, String(mid));
  // samples older than the window are forgotten: the speed is the recent one (3 MB over the last ~2 s)
  r.add(2000, 2_100_000);
  r.add(3000, 2_200_000);
  r.add(3100, 2_300_000);
  const recent = r.rate(3100);
  assert.ok(recent > 100_000 && recent < 300_000, String(recent));
  // the transfer stopped: the number decays toward 0 instead of freezing
  assert.ok(r.rate(4000) < recent);
  assert.equal(r.rate(9000), 0);
  // a new transfer (a smaller total) starts again
  r.add(10_000, 0);
  assert.equal(r.rate(10_000), undefined);
  r.add(11_000, 500_000);
  assert.equal(r.rate(11_000), 500_000);
  r.reset();
  assert.equal(r.rate(11_000), undefined);
});

test('the speed and time-left texts', () => {
  assert.equal(fmtRate(3_100_000), '3.1 MB/s');
  assert.equal(fmtRate(12_400_000), '12 MB/s');
  assert.equal(fmtRate(640_000), '640 KB/s');
  assert.equal(fmtRate(800), '800 B/s');
  assert.equal(fmtEta(12), '12 s left');
  assert.equal(fmtEta(0.2), '1 s left');
  assert.equal(fmtEta(65), '1 min 5 s left');
  assert.equal(fmtEta(7300), 'about 2 h left');
  assert.equal(etaSeconds(6_000_000, 2_000_000), 3);
  assert.equal(etaSeconds(6_000_000, 0), undefined);
  assert.equal(etaSeconds(6_000_000, undefined), undefined);
  assert.equal(statsLine(42, true, 3_000_000, 12_000_000), '42% · 3.0 MB/s · 4 s left');
  assert.equal(statsLine(42, true, undefined, 12_000_000), '42%', 'speed not known yet');
  assert.equal(statsLine(100, false, 3_000_000, 0), '100%', 'a finished job shows no speed');
  assert.equal(statsLine(99, true, 3_000_000, 0), '99% · 3.0 MB/s', 'nothing left to send: no time left');
});

test('when the page reads by itself', () => {
  assert.equal(canAutoRefresh(true, ''), true);
  assert.equal(canAutoRefresh(false, ''), false, 'a hidden window does not read');
  for (const busy of ['Reading', 'Uploading', 'Installing', 'Deleting', 'Restarting'])
    assert.equal(canAutoRefresh(true, busy), false, `not during ${busy}`);
  // focus and visibilitychange fire together: one read
  assert.equal(focusReadDue(undefined, 5000), true);
  assert.equal(focusReadDue(5000, 5000 + FOCUS_GAP_MS - 1), false);
  assert.equal(focusReadDue(5000, 5000 + FOCUS_GAP_MS), true);
  const e = (name, size = 1, kind = 'file') => ({ name, size, kind });
  assert.equal(listingChanged([e('a'), e('b')], [e('b'), e('a')]), false, 'order does not matter');
  assert.equal(listingChanged([e('a')], [e('a'), e('b')]), true, 'a file appeared');
  assert.equal(listingChanged([e('a'), e('b')], [e('a')]), true, 'a file went');
  assert.equal(listingChanged([e('a', 1)], [e('a', 2)]), true, 'a file changed size');
  assert.equal(listingChanged([e('a', 0, 'folder')], [e('a', 0, 'file')]), true);
  assert.equal(listingChanged(undefined, []), true, 'nothing shown yet');
  assert.equal(listingChanged([], []), false);
});

test('"Waiting for ..." shows only after silence, only while a request waits, never during progress', () => {
  const a = createActivity();
  assert.equal(a.quietFor(1000), undefined, 'no request waits');
  assert.equal(waitShown(true, a.quietFor(1000)), false);
  a.touch(1000); // a job starts
  a.recvBegin(); // a request waits for its reply
  assert.equal(a.quietFor(1200), 200);
  assert.equal(waitShown(true, a.quietFor(1000 + WAIT_QUIET_MS - 1)), false, 'a quick answer shows nothing');
  assert.equal(waitShown(true, a.quietFor(1000 + WAIT_QUIET_MS)), true, 'no reply for a while');
  assert.equal(waitShown(false, a.quietFor(9000)), false, 'no job: no notice');
  // an upload that makes progress: replies keep coming, so the silence never reaches the limit
  for (let t = 1000; t < 20_000; t += 400) {
    a.touch(t);
    assert.equal(waitShown(true, a.quietFor(t + 399)), false, `progress at ${t}`);
  }
  // CPU work between requests (a WAV converts) is not waiting for the device
  a.recvEnd();
  assert.equal(a.quietFor(60_000), undefined);
  assert.equal(waitShown(true, a.quietFor(60_000)), false);
  // two requests in flight: waiting until both are over
  a.recvBegin();
  a.recvBegin();
  a.recvEnd();
  assert.notEqual(a.quietFor(60_000), undefined);
  a.recvEnd();
  a.recvEnd(); // an unmatched end does not make the count negative
  a.recvBegin();
  assert.notEqual(a.quietFor(61_000), undefined);
});

test('a transport reports its traffic: a message is activity, a recv in flight is a waiting request', async () => {
  const sent = [];
  let reply = null;
  let release;
  const t = {
    extra: 'kept',
    send: async (m) => void sent.push(m),
    recv: () => new Promise((res) => (release = () => res(reply))),
  };
  const same = instrument(t);
  assert.equal(same, t, 'the transport is changed in place');
  assert.equal(t.extra, 'kept');
  await t.send(Uint8Array.of(1));
  assert.equal(sent.length, 1);
  const pending = t.recv(1000);
  // the module's tracker is shared: a request is now in flight
  const { quietMs } = await import('../../desktop/ui/src/activity.ts');
  assert.notEqual(quietMs(), undefined, 'a recv in flight is a waiting request');
  reply = Uint8Array.of(2);
  release();
  assert.deepEqual(await pending, Uint8Array.of(2));
  assert.equal(quietMs(), undefined, 'nothing waits after the reply');
});

test('the bottom bar of a module job: the bytes sent so far and in all, the message, the fraction', () => {
  let p = startProgress(['a', 'b'], false, { a: 1000, b: 3000 });
  assert.deepEqual([modulesBar(p).sent, modulesBar(p).total, modulesBar(p).message], [0, 4000, 'Starting…']);
  p = applyEvent(p, { event: 'upload', id: 'a', path: 'modules/a.dkm', bytes: 1000 });
  p = applyEvent(p, { event: 'progress', id: 'a', sent: 400, bytes: 1000 });
  assert.deepEqual([modulesBar(p).sent, modulesBar(p).total], [400, 4000]);
  p = applyEvent(p, { event: 'verify', id: 'a', path: 'modules/a.dkm', ok: true });
  assert.equal(modulesBar(p).sent, 1000, 'a verified module counts in full');
  assert.equal(modulesBar(p).failed, false);
  p = applyEvent(p, { event: 'error', message: 'E_FULL' });
  assert.equal(modulesBar(p).failed, false, 'the failed one is b: not yet active, so nothing failed');
  // a restart has no bytes: the bar has a message and a fraction only
  const r = modulesBar(startProgress([], true));
  assert.deepEqual([r.sent, r.total], [0, 0]);
});

test('the bottom bar of a samples upload: only what this run touched, one message for running and one for the end', () => {
  const ev = (u, e) => applyUpload(u, { folder: 'd', ...e });
  let u = {};
  // an earlier run left a verified file
  u = ev(u, { event: 'prepared', name: 'old', source: 'old.wav', kind: 'converted', bytes: 500 });
  u = ev(u, { event: 'verified', name: 'old', ok: true, message: '' });
  const base = u;
  assert.equal(samplesBar(u, base, false), undefined, 'nothing touched: no bar');
  u = ev(u, { event: 'prepared', name: 'k', source: 'k.wav', kind: 'converted', bytes: 1000 });
  u = ev(u, { event: 'prepared', name: 's', source: 's.wav', kind: 'converted', bytes: 3000 });
  let bar = samplesBar(u, base, true);
  assert.deepEqual([bar.sent, bar.total, bar.fraction, bar.message], [0, 4000, 0, 'Preparing…']);
  u = ev(u, { event: 'progress', name: 'k', sent: 500, bytes: 1000 });
  bar = samplesBar(u, base, true);
  assert.deepEqual([bar.sent, bar.fraction, bar.message], [500, 0.125, 'Uploading k (1 of 2)']);
  u = ev(u, { event: 'verifying', name: 'k' });
  assert.equal(samplesBar(u, base, true).message, 'Verifying k (1 of 2)');
  u = ev(u, { event: 'verified', name: 'k', ok: true, message: '' });
  u = ev(u, { event: 'progress', name: 's', sent: 1500, bytes: 3000 });
  bar = samplesBar(u, base, true);
  assert.deepEqual([bar.sent, bar.fraction, bar.message], [2500, 0.625, 'Uploading s (2 of 2)']);
  u = ev(u, { event: 'verified', name: 's', ok: true, message: '' });
  bar = samplesBar(u, base, false);
  assert.deepEqual([bar.fraction, bar.failed, bar.message], [1, false, 'Uploaded 2 files, verified.']);
  // a failure: the bar turns red and the end message counts
  u = ev(u, { event: 'prepared', name: 'x', source: 'x.wav', kind: 'converted', bytes: 100 });
  u = ev(u, { event: 'failed', name: 'x', message: 'not a usable WAV' });
  bar = samplesBar(u, base, false);
  assert.deepEqual([bar.failed, bar.message], [true, '2 of 3 uploaded, 1 failed.']);
  assert.equal(samplesBar(u, base, false).sent, 4000, 'the bytes of a failed file are not counted as sent');
});
