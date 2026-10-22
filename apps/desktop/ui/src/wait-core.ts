// What the pages say while a device has not answered, and after it did not. Pure, so node tests can run it (apps/web/tests/session-cancel.test.mjs).
import { errorLine, isCancelled, isTimeout } from '../../../web/src/modules/backend.ts';

/** How long a request runs before the page shows "Waiting for ..." (a quick answer shows nothing). */
export const WAIT_SHOWN_MS = 600;

export const waitingLine = (target: string): string => `Waiting for ${target}…`;

/** The line for a failed request: no reply names the target, a cancel is not an error, anything else is the error's own text. */
export function failureLine(e: unknown, target: string): string {
  if (isCancelled(e)) return 'Cancelled.';
  if (isTimeout(e))
    return `No reply from ${target}. Check that it is connected and runs the digikit runtime, then press Refresh.`;
  return errorLine(e);
}
