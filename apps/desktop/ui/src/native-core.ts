// The rules of the app's native feel, without the DOM: where text may be selected and copied, and which clicks keep the browser's context
// menu. `SELECTABLE` is also written out in native.css (a node test checks that every selector is there); everything else in the app
// is not selectable. Node tests: apps/web/tests/native-feel.test.mjs.

/** Where text is for copying: text fields, log and console output, paths, error messages. Add `class="selectable"` to anything else. */
export const SELECTABLE = [
  'input',
  'textarea',
  '[contenteditable]',
  '.selectable',
  'pre',
  '.err',
  '.alert',
  '.log',
  '.path',
  '.folder',
  '.rows .name:not(button)',
] as const;

/** What the page does not drag away (a link or an image dragged out of the window). */
export const NO_DRAG = 'a, img';

type Target = { closest?: (selector: string) => unknown } | null | undefined;

const within = (target: Target, selectors: string): boolean => !!target?.closest?.(selectors);

/** The browser's context menu (copy, inspect, reload) stays in text fields and selectable text and is suppressed everywhere else. */
export const contextMenuAllowed = (target: Target): boolean => within(target, SELECTABLE.join(', '));

/** Whether a drag that starts on `target` may go on (a file dropped from outside never starts one here). */
export const dragAllowed = (target: Target): boolean => !within(target, NO_DRAG);
