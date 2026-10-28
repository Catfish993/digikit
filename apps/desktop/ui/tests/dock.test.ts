// The pure logic of the bottom dock (apps/desktop/ui/src/dock-core.ts).
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { DOCK_MIN_PX, clampDock, dockAt, dockMax, parseDock, startDock } from '../src/dock-core.ts';

test('a saved height is used when it is a positive number', () => {
  assert.equal(parseDock('240'), 240);
  for (const bad of [null, undefined, '', 'x', '0', '-5', 'Infinity'])
    assert.equal(parseDock(bad), undefined, String(bad));
});

test('the dock keeps its minimum and leaves room for the page', () => {
  assert.equal(clampDock(10, 900), DOCK_MIN_PX);
  assert.equal(clampDock(5000, 900), dockMax(900));
  assert.equal(clampDock(300, 900), 300);
  // a window too short for both: the minimum wins
  assert.equal(dockMax(300), DOCK_MIN_PX);
});

test('the default is 30% of the window, kept in range', () => {
  assert.equal(startDock(undefined, 900), 270);
  assert.equal(startDock(undefined, 300), DOCK_MIN_PX);
  assert.equal(startDock(2000, 900), dockMax(900));
  assert.equal(startDock(200, 900), 200);
});

test('the pointer sets the height from the bottom edge', () => {
  assert.equal(dockAt(600, 860, 900), 260);
  assert.equal(dockAt(850, 860, 900), DOCK_MIN_PX);
  assert.equal(dockAt(0, 860, 900), dockMax(900));
});
