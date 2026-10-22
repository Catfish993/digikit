// The "select all" checkbox of a pane header (pure, no DOM; apps/web/tests/transfer-page-ui.test.mjs): its three states over the rows
// that are visible, and what a click does. Ticks of rows that are not visible (another folder, the test modules hidden) are left alone.

export type SelectState = 'none' | 'some' | 'all';

/** None, some or all of `keys` (the visible rows that can be ticked) are in `ticked`. No rows: none. */
export function selectState(keys: readonly string[], ticked: ReadonlySet<string>): SelectState {
  const n = keys.filter((k) => ticked.has(k)).length;
  return n === 0 ? 'none' : n === keys.length ? 'all' : 'some';
}

/** The ticks after a click on the header checkbox: all visible rows ticked unless they all are already (then none of them). */
export function toggleAll(keys: readonly string[], ticked: ReadonlySet<string>): Set<string> {
  const next = new Set(ticked);
  if (selectState(keys, ticked) === 'all') for (const k of keys) next.delete(k);
  else for (const k of keys) next.add(k);
  return next;
}
