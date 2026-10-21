// The pure logic of the desktop Transfer page (apps/desktop/ui/src/transfer-core.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DONE_ON_CARD,
  applyEvent,
  applyVerified,
  available,
  eventText,
  installModules,
  onDevice,
  progressFraction,
  startProgress,
  stateText,
  warningText,
} from '../../desktop/ui/src/transfer-core.ts';

const row = (id, group, extra = {}) => ({
  id,
  path: `modules/${(id ?? 'x').split('/').pop()}.dkm`,
  name: id ?? 'x',
  group,
  test: !!id && id.startsWith('test/'),
  version: '0.1.0',
  file_size: 100,
  on_card: 'no',
  loaded: false,
  ...extra,
});
const view = {
  device: 'dt2',
  runtime: '0.2.4',
  free_bytes: 1000,
  loaded: ['digikit/pluck', 'someone/effect'],
  modules: [
    row('digikit/pluck', 'Machines', { loaded: true, on_card: 'yes' }),
    row('test/sine', 'Machines'),
    row('test/dyn-demo', 'Tools', { warnings: ['arena: needs 5, largest free 1'], fits: false }),
  ],
  card_only: [
    { ...row(null, 'Effects', { loaded: true, on_card: 'yes', name: 'someone/effect' }), path: 'modules/effect.dkm' },
    { ...row(null, 'Tools', { name: 'junk' }), path: 'modules/junk.dkm' },
  ],
};

test('the lists: on device is every card file, available is the repo in three groups', () => {
  assert.deepEqual(
    onDevice(view).map((r) => r.path),
    ['modules/effect.dkm', 'modules/junk.dkm', 'modules/pluck.dkm'],
  );
  const a = available(view, false);
  assert.deepEqual(
    a.map((g) => g.group),
    ['Machines', 'Effects', 'Tools'],
  );
  assert.deepEqual(
    a[0].rows.map((r) => r.id),
    ['digikit/pluck'],
  );
  assert.equal(a[1].rows.length, 0); // Effects: no run-time effects yet
  assert.equal(a[2].rows.length, 0); // the test module is hidden
  assert.equal(available(view, true)[2].rows.length, 1);
  assert.equal(available({ ...view, modules: [] }, true)[1].rows.length, 0);
});

test('ticks mean install: only ticked repo modules, in list order, none to start with', () => {
  assert.deepEqual(installModules(view, new Set()), []);
  assert.deepEqual(installModules(view, new Set(['test/sine', 'digikit/pluck'])), ['digikit/pluck', 'test/sine']);
  // a card-only path is never an install
  assert.deepEqual(installModules(view, new Set(['modules/effect.dkm'])), []);
});

test('texts', () => {
  assert.equal(eventText({ event: 'build', id: 'a/b' }), 'Building a/b');
  assert.equal(eventText({ event: 'skip', id: 'a/b' }), 'a/b: already on the card, not uploaded');
  assert.equal(eventText({ event: 'verify', path: 'modules/x.dkm', ok: true }), 'Verified modules/x.dkm on the card');
  assert.equal(eventText({ event: 'done', restarted: false, selected: [] }), DONE_ON_CARD);
  assert.equal(DONE_ON_CARD, 'On the card. Select it in MODULES on the device, then apply and restart.');
  assert.equal(eventText({ event: 'done', restarted: true, loaded: ['x', 'y'] }), 'Restarted. Loaded: x, y');
  assert.equal(eventText({ event: 'done', restarted: true, loaded: [] }), 'Restarted. No module is loaded.');
  assert.equal(eventText({ event: 'error', message: 'paused' }), 'Error: paused');
  assert.equal(stateText({ ...view.modules[0], build: 'needs build' }), 'loaded, needs build');
  assert.equal(stateText(view.modules[1]), 'not on card');
  assert.equal(stateText({ ...view.modules[1], on_card: 'yes' }), 'on card');
  assert.equal(stateText({ ...view.modules[1], on_card: 'other version' }), 'other version on card');
  assert.equal(warningText(view.modules[2]), 'arena: needs 5, largest free 1');
  assert.equal(warningText(view.modules[0]), '');
});

test('the install progress bar: one step per module, no select, green or red', () => {
  let p = startProgress(['a/x', 'a/y'], false);
  assert.equal(p.restart, 'none');
  assert.equal('select' in p, false);
  const ev = (e) => (p = applyEvent(p, e));
  ev({ event: 'upload', id: 'a/x', path: 'modules/x.dkm', bytes: 5 });
  assert.equal(p.modules[0].state, 'upload');
  ev({ event: 'verify', id: 'a/x', path: 'modules/x.dkm', ok: true });
  assert.deepEqual(
    p.modules.map((m) => m.state),
    ['ok', 'wait'],
  );
  ev({ event: 'build', id: 'a/y' });
  ev({ event: 'verify', id: 'a/y', path: 'modules/y.dkm', ok: false });
  assert.deepEqual(
    p.modules.map((m) => m.state),
    ['ok', 'failed'],
  );
  assert.equal(progressFraction(p), 1 / 2);
  // a clean run ends with done (not restarted): every module green
  p = startProgress(['a/x'], false);
  ev({ event: 'upload', id: 'a/x', path: 'p', bytes: 1 });
  ev({ event: 'verify', id: 'a/x', path: 'p', ok: true });
  ev({ event: 'done', restarted: false, selected: [] });
  assert.equal(progressFraction(p), 1);
  assert.equal(p.message, DONE_ON_CARD);
  // an error marks the active step red
  p = startProgress(['a/x'], false);
  ev({ event: 'upload', id: 'a/x', path: 'p', bytes: 1 });
  ev({ event: 'error', message: 'full' });
  assert.equal(p.modules[0].state, 'failed');
});

test('the restart progress: one step, green after the boot, red on an error', () => {
  let p = startProgress([], true);
  const ev = (e) => (p = applyEvent(p, e));
  assert.equal(progressFraction(p), 0);
  ev({ event: 'restart' });
  assert.equal(p.restart, 'build');
  ev({ event: 'done', restarted: true, loaded: [], missing: [] });
  assert.equal(p.restart, 'ok'); // nothing was expected: no error for missing modules
  assert.equal(progressFraction(p), 1);
  p = startProgress([], true);
  ev({ event: 'restart' });
  ev({ event: 'error', message: 'no new boot' });
  assert.equal(p.restart, 'failed');
});

test('verify results are kept by path', () => {
  let v = {};
  v = applyVerified(v, { event: 'verify', path: 'modules/x.dkm', ok: true });
  v = applyVerified(v, { event: 'verify', path: 'modules/y.dkm', ok: false });
  v = applyVerified(v, { event: 'upload', path: 'modules/z.dkm' });
  assert.deepEqual(v, { 'modules/x.dkm': 'ok', 'modules/y.dkm': 'failed' });
});
