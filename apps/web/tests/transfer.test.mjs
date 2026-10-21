// The pure logic of the desktop Transfer panel (apps/desktop/ui/src/transfer-core.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  applyEvent,
  applyVerified,
  available,
  onDevice,
  progressFraction,
  restartPending,
  startProgress,
  differsFromLoaded,
  eventText,
  groupRows,
  initialTicks,
  installRequest,
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

test('groups: three groups always, test modules behind the toggle, card files in their group', () => {
  const g = groupRows(view, false);
  assert.deepEqual(
    g.map((x) => x.group),
    ['Machines', 'Effects', 'Tools'],
  );
  assert.deepEqual(
    g[0].rows.map((r) => r.id),
    ['digikit/pluck'],
  );
  assert.equal(g[1].rows[0].path, 'modules/effect.dkm');
  assert.equal(g[2].rows.length, 1); // junk; the test module is hidden
  const all = groupRows(view, true);
  assert.deepEqual(
    all[0].rows.map((r) => r.id),
    ['digikit/pluck', 'test/sine'],
  );
  assert.equal(all[2].rows.length, 2);
  assert.equal(groupRows({ ...view, modules: [], card_only: [] }, true)[1].rows.length, 0);
});

test('ticks start as what is loaded; the install request splits repo modules and card files', () => {
  const t = initialTicks(view);
  assert.deepEqual([...t].sort(), ['digikit/pluck', 'modules/effect.dkm']);
  assert.equal(differsFromLoaded(view, t), false);
  assert.deepEqual(installRequest(view, t), { modules: ['digikit/pluck'], keep: ['modules/effect.dkm'] });
  t.add('test/sine');
  assert.equal(differsFromLoaded(view, t), true);
  assert.deepEqual(installRequest(view, t).modules, ['digikit/pluck', 'test/sine']);
  t.delete('digikit/pluck');
  assert.deepEqual(installRequest(view, t), { modules: ['test/sine'], keep: ['modules/effect.dkm'] });
});

test('texts', () => {
  assert.equal(eventText({ event: 'build', id: 'a/b' }), 'Building a/b');
  assert.equal(eventText({ event: 'skip', id: 'a/b' }), 'a/b: already on the card, not uploaded');
  assert.equal(eventText({ event: 'done', loaded: ['x', 'y'] }), 'Loaded: x, y');
  assert.equal(eventText({ event: 'error', message: 'paused' }), 'Error: paused');
  assert.equal(stateText({ ...view.modules[0], build: 'needs build' }), 'loaded, needs build');
  assert.equal(stateText(view.modules[1]), 'not on card');
  assert.equal(warningText(view.modules[2]), 'arena: needs 5, largest free 1');
  assert.equal(warningText(view.modules[0]), '');
});

test('state words after an install without a restart', () => {
  const [pluck, sine] = view.modules;
  // nothing selected since the start: the loaded module is just loaded
  assert.equal(stateText(pluck), 'loaded');
  const sel = new Set(['test/sine']);
  const onCard = { ...sine, on_card: 'yes' };
  assert.equal(stateText(onCard, sel), 'on card, loads at restart');
  assert.equal(stateText(pluck, sel), 'loaded, unloads at restart');
  assert.equal(stateText(onCard, new Set()), 'on card');
  assert.equal(restartPending(view, undefined), false);
  assert.equal(restartPending(view, sel), true);
  assert.equal(restartPending(view, initialTicks(view)), false);
});

test('on device and available lists', () => {
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
});

test('the progress bar: one step per module, green or red', () => {
  let p = startProgress(['a/x', 'a/y'], false);
  assert.equal(p.restart, 'none');
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
  assert.equal(progressFraction(p), 1 / 3);
  ev({ event: 'error', message: 'x' });
  assert.equal(p.select, 'failed'); // no active step left: the select did not happen
  // a clean run
  p = startProgress(['a/x'], true);
  ev({ event: 'upload', id: 'a/x', path: 'p', bytes: 1 });
  ev({ event: 'verify', id: 'a/x', path: 'p', ok: true });
  ev({ event: 'select', paths: ['p'] });
  ev({ event: 'restart' });
  assert.equal(p.restart, 'build');
  ev({ event: 'done', restarted: true, loaded: ['a/x'], missing: [] });
  assert.equal(p.restart, 'ok');
  assert.equal(progressFraction(p), 1);
  // a restart that loaded less than asked is red
  p = { ...startProgress([], true), select: 'ok' };
  ev({ event: 'restart' });
  ev({ event: 'done', restarted: true, loaded: [], missing: ['a/x'] });
  assert.equal(p.restart, 'failed');
});

test('verify results are kept by path', () => {
  let v = {};
  v = applyVerified(v, { event: 'verify', path: 'modules/x.dkm', ok: true });
  v = applyVerified(v, { event: 'verify', path: 'modules/y.dkm', ok: false });
  v = applyVerified(v, { event: 'upload', path: 'modules/z.dkm' });
  assert.deepEqual(v, { 'modules/x.dkm': 'ok', 'modules/y.dkm': 'failed' });
});
