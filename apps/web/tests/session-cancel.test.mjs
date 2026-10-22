// Cancel and "no reply" of a DkSession over a transport that never answers (the pending request of a MIDI port with no device behind it),
// and the lines the desktop pages show for them (apps/desktop/ui/src/wait-core.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import { DkError, DkSession } from '../src/modules/dkupload-core.ts';
import { WAIT_QUIET_MS, failureLine, waitingLine } from '../../desktop/ui/src/wait-core.ts';

/** A transport like the native MIDI wait: `recv` ends at its timeout or when the signal aborts, and never delivers a message. */
class SilentTransport {
  sent = 0;
  aborted = 0;
  async send() {
    this.sent++;
  }
  recv(timeoutMs, signal) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(null), timeoutMs);
      signal?.addEventListener(
        'abort',
        () => {
          this.aborted++;
          clearTimeout(timer);
          resolve(null);
        },
        { once: true },
      );
    });
  }
}

/** A transport that ignores the signal (an older one): the session must still end the wait itself. */
class DeafTransport extends SilentTransport {
  recv(timeoutMs) {
    return new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs));
  }
}

const kindOf = (e) => e instanceof DkError && e.kind;
const within = async (p, ms) => {
  const t = setTimeout(() => assert.fail(`still pending after ${ms} ms`), ms);
  try {
    return await p;
  } finally {
    clearTimeout(t);
  }
};

test('cancelPending ends a request that waits for a silent device at once', async () => {
  const t = new SilentTransport();
  const s = new DkSession(t, { timeoutMs: 30_000 });
  const pending = s.info();
  await new Promise((r) => setTimeout(r, 20));
  s.cancelPending();
  await within(
    assert.rejects(pending, (e) => kindOf(e) === 'cancelled'),
    1000,
  );
  assert.equal(t.aborted, 1, 'the transport was told to stop waiting');
  assert.equal(t.sent, 1, 'no retry after a cancel');
});

test('a transport that ignores the signal does not hold the cancel back', async () => {
  const s = new DkSession(new DeafTransport(), { timeoutMs: 30_000 });
  const pending = s.list();
  await new Promise((r) => setTimeout(r, 20));
  s.cancelPending();
  await within(
    assert.rejects(pending, (e) => kindOf(e) === 'cancelled'),
    1000,
  );
});

test('requests queued behind the pending one are cancelled too, and later ones are not', async () => {
  const t = new SilentTransport();
  const s = new DkSession(t, { timeoutMs: 40, retries: 0 });
  const a = s.info();
  const b = s.list();
  const c = s.modules();
  await new Promise((r) => setTimeout(r, 10));
  s.cancelPending();
  for (const p of [a, b, c])
    await within(
      assert.rejects(p, (e) => kindOf(e) === 'cancelled'),
      1000,
    );
  assert.equal(t.sent, 1, 'the queued requests were never sent');
  // after the cancel a new request runs normally: here it times out, which is a different error
  await assert.rejects(s.info(), (e) => kindOf(e) === 'timeout');
  assert.equal(t.sent, 2);
});

test('a device that does not answer ends as a timeout after the retries, not as a hang', async () => {
  const t = new SilentTransport();
  const s = new DkSession(t, { timeoutMs: 25, retries: 2 });
  await within(
    assert.rejects(s.info(), (e) => kindOf(e) === 'timeout'),
    2000,
  );
  assert.equal(t.sent, 3);
});

test('the lines for waiting, no reply and cancel', () => {
  assert.equal(waitingLine('MIDI: IAC Driver Bus 1'), 'Waiting for MIDI: IAC Driver Bus 1…');
  assert.ok(WAIT_QUIET_MS > 0);
  const timeout = new DkError({ error: 'no reply from the device', kind: 'timeout' });
  assert.match(failureLine(timeout, 'MIDI: IAC Driver Bus 1'), /^No reply from MIDI: IAC Driver Bus 1\./);
  assert.doesNotMatch(failureLine(timeout, 'x'), /Refresh/, 'there is no Refresh button to press');
  assert.equal(failureLine(new DkError({ error: 'cancelled', kind: 'cancelled' }), 'x'), 'Cancelled.');
  assert.equal(failureLine(new Error('no MIDI port is open'), 'x'), 'no MIDI port is open');
  assert.equal(failureLine('{"error":"disk full","kind":"device"}', 'x'), 'disk full');
});
