// The Remote page's logic, without the page: what the state of digikit/remote says to the user, the device behind a session, and the
// runtime object the faceplate presses keys on. Pure, so host tests can run it (apps/desktop/ui/tests/remote-page.test.ts).
import { DkError, type DkSession, type RemoteState } from '@digi/dk-session';

/** The message under the faceplate for the state of digikit/remote (or for the error that asking for it gave). */
export function remoteNotice(state: RemoteState | undefined, error?: unknown): string {
  if (error instanceof DkError) {
    if (error.status === 'E_PROTO')
      return 'digikit/remote is not loaded on this device. Install it on the Transfer page, select it in MODULES on the device, apply and restart.';
    return error.message;
  }
  if (error) return String(error);
  if (!state) return 'Reading the state of digikit/remote…';
  if (!state.enabled)
    return 'digikit/remote reports itself off. On the device, check it in SETTINGS > DIGIKIT > MODULES, apply and restart.';
  return '';
}

/** What the faceplate calls to press, release and turn: each becomes one request of the remote module. */
export function remoteRuntime(get: () => Promise<DkSession>) {
  return {
    press: async (code: number) => (await get()).remoteKey(code, true),
    release: async (code: number) => (await get()).remoteKey(code, false),
    turn: async (encoder: number, detents: number) => (await get()).remoteTurn(encoder, detents),
  };
}

/** `dt2` or `dn2` from the INFO port name of a session (`dt2`, or `dt2-1.17`), or undefined when it is another device. */
export function deviceOfPort(port: string): 'dt2' | 'dn2' | undefined {
  const d = port.split('-')[0];
  return d === 'dt2' || d === 'dn2' ? d : undefined;
}
