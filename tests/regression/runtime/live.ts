import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { DkSession, EmuUsbTransport } from '@digi/dk-session';
import { repoRoot, type Device, type Session } from '@digi/test-support';
import { expect } from 'vitest';

export function liveOptions(device: Device) {
  const binary = process.env[device === 'dt2' ? 'DIGI_EMUD_DT2' : 'DIGI_EMUD_DN2'];
  if (!binary) throw new Error(`DIGI_EMUD_${device.toUpperCase()} must name a preflighted daemon`);
  const card = device === 'dt2' ? process.env.DIGI_RUNTIME_CARD : undefined;
  if (device === 'dt2' && !card) throw new Error('DIGI_RUNTIME_CARD must name the generated DT2 sample directory');
  return { device, firmware: device === 'dt2' ? '1.17' : '1.12', binary, card, mirror: true };
}

export async function settleLive(session: Session, ms = 12_000): Promise<void> {
  await delay(ms);
  expect((await session.status()).mode, 'the daemon runs freely').toBe('free');
}

/** New protocol and USB receive state; deliberately retain the daemon/card/boot history. */
export function liveProtocol(session: Session): DkSession {
  return new DkSession(new EmuUsbTransport((method, params) => session.client.call(method, params)), {
    timeoutMs: 20_000,
    chunk: 4096,
  });
}

const artifactDirectories = new WeakMap<Session, Map<string, string>>();

export function liveArtifacts(session: Session, workflow: string): string {
  let workflows = artifactDirectories.get(session);
  if (!workflows) {
    workflows = new Map();
    artifactDirectories.set(session, workflows);
  }
  const existing = workflows.get(workflow);
  if (existing) return existing;
  const root = path.join(repoRoot(), 'out/regression', workflow, session.options.device);
  fs.mkdirSync(root, { recursive: true });
  const dir = fs.mkdtempSync(path.join(root, 'run-'));
  workflows.set(workflow, dir);
  console.info('runtime artifacts:', dir);
  return dir;
}

export function liveCheck(name: string, ok: unknown, detail: unknown = ''): void {
  expect(Boolean(ok), `${name}: ${String(detail)}`).toBe(true);
}

/** 0.1 seconds at 44.1 kHz exercises the converter's resampling path. */
export function sampleWav(channels: 1 | 2): Uint8Array {
  const frames = 4410;
  const pcm = Buffer.alloc(frames * channels * 2);
  for (let i = 0; i < frames; i++) {
    pcm.writeInt16LE(Math.round(12000 * Math.sin((2 * Math.PI * 440 * i) / 44100)), i * channels * 2);
    if (channels === 2) pcm.writeInt16LE(Math.round(9000 * Math.sin((2 * Math.PI * 660 * i) / 44100)), i * 4 + 2);
  }
  const h = Buffer.alloc(44);
  h.write('RIFF', 0);
  h.writeUInt32LE(36 + pcm.length, 4);
  h.write('WAVEfmt ', 8);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(channels, 22);
  h.writeUInt32LE(44100, 24);
  h.writeUInt32LE(44100 * channels * 2, 28);
  h.writeUInt16LE(channels * 2, 32);
  h.writeUInt16LE(16, 34);
  h.write('data', 36);
  h.writeUInt32LE(pcm.length, 40);
  return Uint8Array.from(Buffer.concat([h, pcm]));
}
