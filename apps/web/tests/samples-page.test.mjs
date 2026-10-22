// The pure logic of the Samples tab of the desktop Transfer page (apps/desktop/ui/src/samples-core.ts): tabs, folders and breadcrumb, the
// rows with their upload states, which files convert (the labels), and an upload run over a fake backend.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileGroups, nameInGroup } from '../../desktop/ui/src/card-core.ts';
import {
  TABS,
  applyUpload,
  crumbs,
  deleteRows,
  dropUploads,
  folderNameError,
  joinPath,
  kindLabel,
  parentOf,
  parseTab,
  sampleRows,
  spaceText,
  storeNote,
  uploadFiles,
  uploadStatus,
} from '../../desktop/ui/src/samples-core.ts';

const file = (name, size = 10) => ({ name, kind: 'file', size });
const folder = (name) => ({ name, kind: 'folder', size: 0 });

test('the tabs: Modules and Samples, Modules for anything unknown', () => {
  assert.deepEqual(
    TABS.map((t) => t.label),
    ['Modules', 'Samples'],
  );
  assert.equal(parseTab('samples'), 'samples');
  assert.equal(parseTab('modules'), 'modules');
  assert.equal(parseTab(null), 'modules');
  assert.equal(parseTab('card'), 'modules');
});

test('folder paths and the breadcrumb', () => {
  assert.equal(joinPath('', 'kick'), 'kick');
  assert.equal(joinPath('drums/808', 'kick'), 'drums/808/kick');
  assert.equal(parentOf('drums/808'), 'drums');
  assert.equal(parentOf('drums'), '');
  assert.equal(parentOf(''), '');
  assert.deepEqual(crumbs(''), [{ name: 'Samples', path: '' }]);
  assert.deepEqual(crumbs('drums/808'), [
    { name: 'Samples', path: '' },
    { name: 'drums', path: 'drums' },
    { name: '808', path: 'drums/808' },
  ]);
});

test('a new folder name', () => {
  const here = [folder('drums'), file('kick')];
  assert.equal(folderNameError('loops', here), '');
  assert.match(folderNameError('', here), /name/);
  assert.match(folderNameError('  ', here), /name/);
  assert.match(folderNameError('a/b', here), /Not a folder name/);
  assert.match(folderNameError('..', here), /Not a folder name/);
  assert.match(folderNameError(' x', here), /Not a folder name/);
  assert.match(folderNameError('drums', here), /exists already/);
  assert.match(folderNameError('kick', here), /exists already/);
});

test('rows: folders first, then files by name; an upload that is not listed yet is a pending row', () => {
  const uploads = {
    'drums/new': {
      folder: 'drums',
      name: 'new',
      source: 'new.wav',
      kind: 'converted',
      state: 'uploading',
      sent: 5,
      bytes: 20,
      message: '',
    },
    'other/x': {
      folder: 'other',
      name: 'x',
      source: 'x',
      kind: 'unchanged',
      state: 'waiting',
      sent: 0,
      bytes: 1,
      message: '',
    },
    'drums/snare': {
      folder: 'drums',
      name: 'snare',
      source: 'snare.wav',
      kind: 'converted',
      state: 'ok',
      sent: 9,
      bytes: 9,
      message: 'device content hash matches\nthe firmware hash',
    },
  };
  const rows = sampleRows('drums', [file('snare', 9), folder('808'), file('kick')], uploads);
  assert.deepEqual(
    rows.map((r) => [r.name, r.path, r.kind, r.pending]),
    [
      ['808', 'drums/808', 'folder', false],
      ['kick', 'drums/kick', 'file', false],
      ['new', 'drums/new', 'file', true],
      ['snare', 'drums/snare', 'file', false],
    ],
  );
  assert.equal(rows[1].status, undefined);
  assert.deepEqual(rows[2].status, { kind: 'uploading', sent: 5, bytes: 20, message: '' });
  assert.deepEqual(rows[3].status, { kind: 'ok', message: 'device content hash matches\nthe firmware hash' });
});

test('the state of an upload: waiting, uploading, verifying, ok, failed', () => {
  const u = (state, sent = 0, bytes = 10, message = '') => ({
    folder: '',
    name: 'a',
    source: 'a',
    kind: 'native',
    state,
    sent,
    bytes,
    message,
  });
  assert.equal(uploadStatus(undefined), undefined);
  assert.equal(uploadStatus(u('waiting')).kind, 'waiting');
  assert.equal(uploadStatus(u('uploading', 3)).kind, 'uploading');
  assert.equal(uploadStatus(u('uploading', 10)).kind, 'verifying'); // all sent: the device is checking
  assert.equal(uploadStatus(u('verifying', 10)).kind, 'verifying');
  assert.equal(uploadStatus(u('ok', 10, 10, 'm')).message, 'm');
  assert.equal(uploadStatus(u('failed', 0, 10, 'no space')).kind, 'failed');
});

test('events build the upload state, and a file that could not be prepared fails alone', () => {
  let u = {};
  u = applyUpload(u, {
    event: 'prepared',
    folder: 'd',
    name: 'kick',
    source: 'kick.wav',
    kind: 'converted',
    bytes: 100,
  });
  u = applyUpload(u, { event: 'progress', folder: 'd', name: 'kick', sent: 40, bytes: 100 });
  assert.equal(u['d/kick'].state, 'uploading');
  u = applyUpload(u, { event: 'verifying', folder: 'd', name: 'kick' });
  assert.deepEqual([u['d/kick'].state, u['d/kick'].sent], ['verifying', 100]);
  u = applyUpload(u, { event: 'verified', folder: 'd', name: 'kick', ok: false, message: 'device sha256 differs' });
  assert.deepEqual([u['d/kick'].state, u['d/kick'].message], ['failed', 'device sha256 differs']);
  u = applyUpload(u, { event: 'failed', folder: 'd', name: 'bad.wav', source: 'bad.wav', message: 'not a usable WAV' });
  assert.deepEqual([u['d/bad.wav'].kind, u['d/bad.wav'].state, u['d/bad.wav'].source], ['none', 'failed', 'bad.wav']);
  assert.deepEqual(Object.keys(dropUploads(u, ['d/kick'])), ['d/bad.wav']);
});

test('which files convert: the label says what was done', () => {
  assert.equal(kindLabel('converted'), 'WAV, converted');
  assert.equal(kindLabel('native'), 'native sample');
  assert.equal(kindLabel('unchanged'), "won't play as a sample");
  assert.equal(kindLabel('none'), '');
});

test('delete: the ticked rows that are on the device; the space line; the store note', () => {
  const rows = sampleRows('', [folder('a'), file('b'), file('c')], {
    p: { folder: '', name: 'p', source: 'p', kind: 'native', state: 'waiting', sent: 0, bytes: 1, message: '' },
  });
  assert.deepEqual(
    deleteRows(rows, new Set(['c', 'a', 'p', 'gone'])).map((r) => r.path),
    ['a', 'c'],
  );
  assert.equal(spaceText(undefined), '');
  assert.equal(spaceText({ free: 2048, total: null }), '2.0 KiB free');
  assert.equal(spaceText({ free: 1024 * 1024, total: 4 * 1024 * 1024 }), '1.00 MiB free of 4.00 MiB');
  assert.equal(
    storeNote('digikit', 'Digitone II'),
    "Stored for a future sampling engine; the Digitone II can't play samples yet.",
  );
  assert.equal(storeNote('stock-rpc', 'Digitakt II'), '');
});

/** A backend that keeps what it is given; `failPut` names a file whose put throws, `failVerify` one whose verify says no. */
const fakeBackend = ({ failPut = '', failVerify = '' } = {}) => {
  const calls = [];
  return {
    calls,
    async put(folderName, p, onProgress) {
      calls.push(`put ${folderName}/${p.name}`);
      if (p.name === failPut) throw new Error('{"error":"E_FULL: no space","kind":"device"}');
      onProgress(p.stored.length / 2, p.stored.length);
      onProgress(p.stored.length, p.stored.length);
    },
    async verify(folderName, p) {
      calls.push(`verify ${folderName}/${p.name}`);
      return {
        ok: p.name !== failVerify,
        message: p.name === failVerify ? 'device sha256 differs' : 'device sha256 matches',
        compared: 'sha256 of the stored file',
      };
    },
  };
};
const prepare = (name, data) => {
  if (name.endsWith('.bad.wav')) throw new Error('not a usable WAV: no fmt chunk');
  const wav = name.endsWith('.wav');
  return {
    name: wav ? name.replace(/\.wav$/, '') : name,
    bytes: data,
    stored: data,
    kind: wav ? 'converted' : 'unchanged',
  };
};
const run = async (backend, names) => {
  let u = {};
  await uploadFiles(
    backend,
    prepare,
    'drums',
    names.map((n) => ({ name: n, data: new Uint8Array(10) })),
    (e) => (u = applyUpload(u, e)),
  );
  return u;
};

test('an upload run: prepare, put with progress, verify; a broken WAV fails alone', async () => {
  const b = fakeBackend({ failVerify: 'snare' });
  const u = await run(b, ['kick.wav', 'x.bad.wav', 'snare.wav', 'notes.txt']);
  assert.deepEqual(
    Object.values(u).map((x) => [x.name, x.kind, x.state]),
    [
      ['kick', 'converted', 'ok'],
      ['x.bad.wav', 'none', 'failed'],
      ['snare', 'converted', 'failed'],
      ['notes.txt', 'unchanged', 'ok'],
    ],
  );
  assert.equal(u['drums/kick'].message, 'device sha256 matches\nsha256 of the stored file');
  assert.match(u['drums/x.bad.wav'].message, /not a usable WAV/);
  assert.equal(u['drums/snare'].message, 'device sha256 differs\nsha256 of the stored file');
  assert.deepEqual(b.calls, [
    'put drums/kick',
    'verify drums/kick',
    'put drums/snare',
    'verify drums/snare',
    'put drums/notes.txt',
    'verify drums/notes.txt',
  ]);
});

test('a failed send stops the run: the files after it are marked not sent', async () => {
  const b = fakeBackend({ failPut: 'two' });
  const u = await run(b, ['one.wav', 'two.wav', 'three.wav']);
  assert.deepEqual(
    Object.values(u).map((x) => [x.name, x.state]),
    [
      ['one', 'ok'],
      ['two', 'failed'],
      ['three', 'failed'],
    ],
  );
  assert.match(u['drums/two'].message, /E_FULL: no space/);
  assert.equal(u['drums/three'].message, 'not sent: the upload of two failed');
  assert.deepEqual(b.calls, ['put drums/one', 'verify drums/one', 'put drums/two']);
});

test('the Card panel groups the store files by top folder', () => {
  const card = {
    folder: 'x',
    continuous: true,
    last_sync: null,
    error: null,
    files: [
      { path: 'samples/drums/kick', bytes: 300, sha256: 'c' },
      { path: 'modules/pluck.dkm', bytes: 100, sha256: 'a' },
      { path: 'samples/snare', bytes: 50, sha256: 'b' },
      { path: 'top.bin', bytes: 1, sha256: 'd' },
    ],
  };
  const g = fileGroups(card);
  assert.deepEqual(
    g.map((x) => [x.folder, x.files.length, x.bytes]),
    [
      ['', 1, 1],
      ['modules', 1, 100],
      ['samples', 2, 350],
    ],
  );
  assert.equal(nameInGroup('samples/drums/kick', 'samples'), 'drums/kick');
  assert.equal(nameInGroup('top.bin', ''), 'top.bin');
});
