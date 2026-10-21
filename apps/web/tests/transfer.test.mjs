// The pure logic of the desktop Transfer panel (apps/desktop/ui/src/transfer-core.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import {
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
  assert.equal(stateText({ ...view.modules[0], build: 'needs build' }), 'loaded, on card, needs build');
  assert.equal(stateText(view.modules[1]), 'not on card');
  assert.equal(warningText(view.modules[2]), 'arena: needs 5, largest free 1');
  assert.equal(warningText(view.modules[0]), '');
});
