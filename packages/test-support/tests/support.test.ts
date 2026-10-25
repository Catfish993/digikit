import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { BUTTONS, button, encoder, golden, validateRuntimeInputs } from '../src/index.ts';

test('key names follow the Python harness table', () => {
  assert.equal(button('trig_1'), 25);
  assert.equal(button('Trig 16'), 40);
  assert.equal(button('push h'), 48);
  assert.equal(button('page trig'), 1);
  assert.equal(button(7), 7);
  assert.equal(encoder('enc a'), 1);
  assert.equal(encoder('LEVEL'), 9);
  assert.equal(Object.keys(BUTTONS).length, 26 + 16 + 8);
  assert.throws(() => button('nope'));
});

test('golden writes only under UPDATE_GOLDENS=1', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-golden-'));
  const file = path.join(dir, 'g.json');
  const prev = process.env.UPDATE_GOLDENS;
  try {
    delete process.env.UPDATE_GOLDENS;
    assert.throws(() => golden(file, 'a', 1));
    process.env.UPDATE_GOLDENS = '1';
    golden(file, 'a', { x: 1 });
    delete process.env.UPDATE_GOLDENS;
    golden(file, 'a', { x: 1 });
    assert.throws(() => golden(file, 'a', { x: 2 }));
  } finally {
    if (prev === undefined) delete process.env.UPDATE_GOLDENS;
    else process.env.UPDATE_GOLDENS = prev;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('runtime provenance accepts matching stock bytes and rejects forced or stale ready inputs', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'digi-provenance-'));
  const syx = path.join(dir, 'stock.syx');
  fs.writeFileSync(syx, 'stock fixture');
  const inputs = { syx, readyDir: dir, cfSnapshot: '', dspImage: '', dspState: '' };
  const source = { syx_sha256: createHash('sha256').update('stock fixture').digest('hex'), intc_force: false };
  const write = (value: object) => fs.writeFileSync(path.join(dir, 'SOURCE.json'), JSON.stringify(value));
  try {
    write(source);
    validateRuntimeInputs(inputs);
    write({ ...source, intc_force: true });
    assert.throws(() => validateRuntimeInputs(inputs), /provenance mismatch/);
    write({ ...source, syx_sha256: 'stale' });
    assert.throws(() => validateRuntimeInputs(inputs), /provenance mismatch/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
