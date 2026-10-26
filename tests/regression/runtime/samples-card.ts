import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { repoRoot, type Session } from '@digi/test-support';
import { expect } from 'vitest';

const exec = promisify(execFile);

/** Independent +Drive reader/builder oracle over the guest's card writes. No implicit builds. */
export async function expectSampleCard(session: Session, out: string, hash: number): Promise<void> {
  const plusdrive = process.env.DIGI_PLUSDRIVE;
  if (!plusdrive) throw new Error('DIGI_PLUSDRIVE must name an existing plusdrive binary');
  const base = session.cardPath;
  if (!base) throw new Error('sample card oracle requires the loaded base card image');
  const overlay = path.join(out, 'samples.ov.bin');
  await session.client.call('card.overlay', { path: overlay });
  const after = path.join(out, 'samples-after.img');
  await exec('uv', ['run', 'python', '-m', 'tools.emulator.card_overlay', 'apply', base, overlay, after], {
    cwd: repoRoot(),
    timeout: 30_000,
  });
  const run = (args: string[]) =>
    exec(plusdrive, args, { cwd: repoRoot(), timeout: 30_000, encoding: 'buffer', maxBuffer: 4 * 1024 * 1024 });
  const got = (await run(['fs', 'cat', after, '/live/test'])).stdout;
  const sent = fs.readFileSync(path.join(out, 'sent-test.bin'));
  expect(got, 'the card holds sent bytes plus a 16-byte zero trailer').toEqual(Buffer.concat([sent, Buffer.alloc(16)]));
  const folder = path.join(out, 'built/plus_drive/dt2');
  fs.mkdirSync(folder, { recursive: true });
  fs.copyFileSync(path.join(out, 'test.wav'), path.join(folder, 'test.wav'));
  const built = path.join(out, 'built.img');
  await run(['build', folder, '-o', built, '--device', 'dt2', '--no-project']);
  const want = (await run(['fs', 'cat', built, '/test'])).stdout;
  expect(want.length).toBeGreaterThan(0x50);
  expect(got, 'the card equals the +Drive builder file for the same WAV').toEqual(want);
  const listing = (await run(['fs', 'ls', after])).stdout.toString();
  fs.writeFileSync(path.join(out, 'card-listing.txt'), listing);
  const row = listing
    .split('\n')
    .find((line) => line.startsWith('live/test\t'))
    ?.split('\t');
  expect(row, 'card reader lists the uploaded sample').toHaveLength(3);
  expect(parseInt(row![2]!, 16), 'record hash|1 matches the firmware hash').toBe(hash);
  expect(Number(row![1]), 'record size includes the trailer').toBe(sent.length + 16);
  expect(
    listing.split('\n').some((line) => line.startsWith('live/notes.txt') || line.startsWith('live/renamed.txt')),
    'card keeps no deleted text file',
  ).toBe(false);
}
