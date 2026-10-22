// The native-feel rules of the desktop app (apps/desktop/ui/src/native-core.ts, native.css).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { NO_DRAG, SELECTABLE, contextMenuAllowed, dragAllowed } from '../../desktop/ui/src/native-core.ts';

/** A stand-in for an element: `closest(selector)` finds `self` when the selector list names one of its `matches`. */
const el = (...matches) => ({
  closest: (selector) => (selector.split(',').some((s) => matches.includes(s.trim())) ? {} : null),
});

test('the context menu stays in text fields and selectable text only', () => {
  for (const s of ['input', 'textarea', '.selectable', 'pre', '.err', '.log', '.path'])
    assert.ok(contextMenuAllowed(el(s)), s);
  assert.equal(contextMenuAllowed(el('button')), false);
  assert.equal(contextMenuAllowed(el()), false);
  assert.equal(contextMenuAllowed(null), false);
  assert.equal(contextMenuAllowed({}), false, 'not an element');
});

test('links and images are not dragged, anything else may start a drag', () => {
  assert.equal(dragAllowed(el('a')), false);
  assert.equal(dragAllowed(el('img')), false);
  assert.equal(dragAllowed(el('button')), true);
  assert.deepEqual(
    NO_DRAG.split(',').map((s) => s.trim()),
    ['a', 'img'],
  );
});

test('native.css makes every selectable selector selectable, and the rest of the app not', () => {
  const css = readFileSync(new URL('../../desktop/ui/src/native.css', import.meta.url), 'utf8');
  const rule = (name) => css.slice(css.indexOf(name), css.indexOf('}', css.indexOf(name)));
  const selectable = css.slice(
    css.indexOf('/* selectable text */'),
    css.indexOf('}', css.indexOf('/* selectable text */')),
  );
  for (const s of SELECTABLE) assert.ok(selectable.includes(s), `${s} is not in the selectable rule of native.css`);
  assert.match(selectable, /user-select: text/);
  assert.match(rule('body {\n  cursor'), /user-select: none/);
  assert.match(rule('html,\nbody {'), /overscroll-behavior: none/);
  assert.match(css, /:focus:not\(:focus-visible\)\s*\{\s*outline: none/);
  assert.match(rule('a,\nimg {'), /-webkit-user-drag: none/);
});
