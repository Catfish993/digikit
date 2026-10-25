// The pure logic of the desktop Remote page (apps/desktop/ui/src/remote-core.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import { DkError } from '@digi/dk-session';
import { deviceOfPort, remoteNotice, remoteRuntime } from '../../desktop/ui/src/remote-core.ts';

test('the notice: nothing when REMOTE is on, how to switch it on, how to load the module', () => {
  assert.equal(remoteNotice({ enabled: true, format: 1, turnLimit: 30 }), '');
  assert.match(remoteNotice({ enabled: false, format: 1, turnLimit: 30 }), /SETTINGS > DIGIKIT > REMOTE/);
  assert.match(remoteNotice(undefined), /Reading/);
  const noModule = new DkError({ error: 'remote state: E_PROTO', kind: 'device', status: 'E_PROTO' });
  assert.match(remoteNotice(undefined, noModule), /not loaded.*Transfer page/);
  const other = new DkError({ error: 'no reply from the device', kind: 'timeout' });
  assert.equal(remoteNotice(undefined, other), 'no reply from the device');
});

test('the device behind a session', () => {
  assert.equal(deviceOfPort('dt2'), 'dt2');
  assert.equal(deviceOfPort('dn2-1.12'), 'dn2');
  assert.equal(deviceOfPort('other'), undefined);
});

test('the runtime of the faceplate sends one request per key and turn', async () => {
  const calls = [];
  const session = {
    remoteKey: async (code, down) => calls.push(['key', code, down]),
    remoteTurn: async (enc, d) => calls.push(['turn', enc, d]),
  };
  const rt = remoteRuntime(async () => session);
  await rt.press(10);
  await rt.release(10);
  await rt.turn(3, -4);
  assert.deepEqual(calls, [
    ['key', 10, true],
    ['key', 10, false],
    ['turn', 3, -4],
  ]);
});
