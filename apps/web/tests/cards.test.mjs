import assert from 'node:assert/strict';
import test from 'node:test';
import { cardLine, folderName, startCard, topLevelWavs } from '../src/runtime/cards.ts';

const file = (name, path, bytes = [1, 2, 3]) => ({
  name,
  webkitRelativePath: path,
  arrayBuffer: async () => new Uint8Array(bytes).buffer,
});

test('only .wav files directly in the chosen folder are used, sorted by name', () => {
  const list = [
    file('b.wav', 'drums/b.wav'),
    file('A.WAV', 'drums/A.WAV'),
    file('note.txt', 'drums/note.txt'),
    file('deep.wav', 'drums/sub/deep.wav'),
    file('.hidden.wav', 'drums/.hidden.wav'),
    file('loose.wav', ''),
  ];
  assert.deepEqual(
    topLevelWavs(list).map((f) => f.name),
    ['A.WAV', 'b.wav', 'loose.wav'],
  );
  assert.deepEqual(topLevelWavs([]), []);
});

test('the toolbar line follows the controller status', () => {
  assert.equal(cardLine(undefined), '');
  assert.equal(cardLine({ state: 'idle' }), 'Choose samples to load a folder of WAVs');
  assert.equal(cardLine({ state: 'error', message: 'no .wav files' }), 'Card failed: no .wav files');
  assert.equal(
    cardLine({ state: 'working', message: 'Building the card (2/3 samples)', fraction: 0.07 }),
    'Building the card (2/3 samples) (7%)',
  );
  assert.equal(
    cardLine({ state: 'working', message: 'Booting the Digitakt II with the card (42%)', fraction: 0.42 }),
    'Booting the Digitakt II with the card (42%)',
  );
  assert.equal(cardLine({ state: 'ready', samples: 3, name: 'Kick drums' }), 'Samples: Kick drums (3 files)');
  assert.equal(cardLine({ state: 'ready', samples: 1, ready_cached: true }), 'Samples: card (1 file)');
});

test('startCard resets, stages every file in order, then starts the job', async () => {
  const calls = [];
  const bridge = {
    reset: async () => void calls.push('reset'),
    stage: async (name, bytes) => void calls.push(`stage ${name} ${bytes.length}`),
    load: async (name) => void calls.push(`load ${name}`),
    status: async () => ({ state: 'idle' }),
  };
  const progress = [];
  await startCard(bridge, [file('a.wav', 'x/a.wav', [1]), file('b.wav', 'x/b.wav', [1, 2])], (done, total) =>
    progress.push([done, total]),
  );
  assert.deepEqual(calls, ['reset', 'stage a.wav 1', 'stage b.wav 2', 'load x']);
  assert.deepEqual(progress, [
    [1, 2],
    [2, 2],
  ]);
});

test('the folder name is the first path part of the chosen files', () => {
  assert.equal(folderName([file('a.wav', 'Kick drums/a.wav')]), 'Kick drums');
  assert.equal(folderName([file('a.wav', '')]), '');
});
