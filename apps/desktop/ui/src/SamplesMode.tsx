// Transfer page, Samples mode. On your computer: a folder of samples (chosen once, remembered; one level shown with a breadcrumb) and the files
// added by hand or dropped (kept in the list even when they are outside the folder). On device: the device's sample folders. The backend is the
// one the device profile names (`[storage] samples`: the stock MidiRpc file commands or the digikit store; samples-backend.ts), so the mode is
// the same on every device. A WAV is converted to the native sample file before it goes; a native sample goes as it is; any other file is
// uploaded unchanged. Each upload is verified against the bytes sent. The page speaks the protocol in TypeScript over the selected target; the
// local files come through the native `local_*` commands (local.rs); no process is started.
import { For, Show, createSignal, type JSX } from 'solid-js';
import { errorLine, formatBytes } from '@web/modules/backend';
import { openDeviceSamples, labelOf, pickFolder, readLocalFile } from './samples-backend';
import {
  applyUpload,
  crumbs,
  deleteRows,
  dropUploads,
  folderNameError,
  joinPath,
  localCrumbs,
  localRowTags,
  localRows,
  pickedRows,
  sampleRows,
  storeNote,
  uploadFiles,
  type SampleRow,
} from './samples-core';
import {
  Crumbs,
  DevicePane,
  DeleteButton,
  ListRow,
  LocalPane,
  SelectAll,
  createConfirm,
  type PageCtx,
} from './TransferParts';
import { noteActivity } from './activity';
import { countLabel } from './transfer-core';
import SplitPane from './SplitPane';
import {
  addedFiles,
  deviceInfo,
  localEntries,
  localFolder,
  localKinds,
  localPath,
  localTicks,
  sampleEntries,
  sampleFolder,
  sampleTicks,
  setAddedFiles,
  setLocalFolder,
  setLocalKinds,
  setLocalPath,
  setLocalTicks,
  setSampleFolder,
  setSampleTicks,
  setUploadBase,
  setUploads,
  uploads,
} from './transfer-state';

export default function SamplesMode(props: { ctx: PageCtx }): JSX.Element {
  const { busy, run, setError } = props.ctx;
  const [note, setNote] = createSignal('');
  const [naming, setNaming] = createSignal(false);
  const [newName, setNewName] = createSignal('');
  let input: HTMLInputElement | undefined;
  const confirm = createConfirm();

  // ---- On your computer ------------------------------------------------------------------------------------------------------------
  const lrows = () =>
    localRows(
      localPath(),
      localEntries() ?? [],
      addedFiles().map((f) => ({ name: f.name, size: f.size })),
      localKinds(),
    );
  const picked = () => pickedRows(lrows(), localTicks());
  const flip = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  const choose = async () => {
    try {
      const dir = await pickFolder(localFolder());
      if (!dir) return;
      setLocalFolder(dir);
      setLocalPath('');
      setLocalTicks(new Set<string>());
      void run('Reading', async () => {});
    } catch (e) {
      setError(errorLine(e));
    }
  };
  const openLocal = (path: string) => {
    if (busy()) return;
    setLocalPath(path);
    setLocalTicks(new Set<string>());
    void run('Reading', async () => {});
  };
  /** Files added by hand join the list (same name: the newer one) and are ticked; their first bytes tell what the upload will do. */
  const addFiles = (files: File[]) => {
    if (!files.length) return;
    const names = new Set(files.map((f) => f.name));
    setAddedFiles((old) => [...old.filter((f) => !names.has(f.name)), ...files]);
    setLocalTicks((t) => new Set([...t, ...files.map((f) => `+${f.name}`)]));
    for (const f of files)
      void labelOf(async () => new Uint8Array(await f.slice(0, 64).arrayBuffer()), f.size).then((k) => {
        if (k) setLocalKinds((all) => ({ ...all, [`+${f.name}`]: k }));
      });
  };
  const upload = () =>
    run('Uploading', async () => {
      const o = await openDeviceSamples();
      const rows = picked();
      const data = await Promise.all(
        rows.map(async (r) => ({
          name: r.name,
          data: r.added
            ? new Uint8Array(await (addedFiles().find((f) => f.name === r.name) as File).arrayBuffer())
            : await readLocalFile(r.path),
        })),
      );
      setNote('');
      setUploadBase(uploads());
      await uploadFiles(o.backend, o.prepare, sampleFolder(), data, (e) => {
        noteActivity();
        setUploads((u) => applyUpload(u, e));
      });
      setLocalTicks(new Set<string>());
    });

  // ---- On device -------------------------------------------------------------------------------------------------------------------
  const open = (folder: string) => {
    if (busy()) return;
    setSampleFolder(folder);
    setSampleTicks(new Set<string>());
    setNote('');
    confirm.stop();
    void run('Reading', async () => {});
  };
  const createFolder = () => {
    const name = newName().trim();
    const why = folderNameError(name, sampleEntries() ?? []);
    if (why) {
      setError(why);
      return;
    }
    setNaming(false);
    setNewName('');
    void run('Creating', async () => {
      await (await openDeviceSamples()).backend.mkdir(joinPath(sampleFolder(), name));
    });
  };
  const rows = () => sampleRows(sampleFolder(), sampleEntries() ?? [], uploads());
  const nDelete = () => deleteRows(rows(), sampleTicks()).length;
  const removeSelected = () =>
    run('Deleting', async () => {
      const { backend } = await openDeviceSamples();
      const gone = deleteRows(rows(), sampleTicks());
      for (const r of gone) {
        if (r.kind === 'folder') await backend.rmdir(r.path);
        else await backend.remove(sampleFolder(), r.name);
        setUploads((u) => dropUploads(u, [r.path]));
        setSampleTicks((t) => {
          const next = new Set(t);
          next.delete(r.path);
          return next;
        });
      }
      setNote(`Deleted ${gone.length} item${gone.length === 1 ? '' : 's'}.`);
    });
  const toggle = (r: SampleRow) => {
    setSampleTicks(flip(sampleTicks(), r.path));
    confirm.stop();
  };

  return (
    <SplitPane id="transfer">
      <LocalPane
        onDrop={addFiles}
        selectAll={
          <SelectAll
            label="Select all files to upload"
            keys={lrows()
              .filter((r) => r.kind === 'file')
              .map((r) => r.key)}
            ticked={localTicks()}
            disabled={!!busy()}
            onChange={setLocalTicks}
          />
        }
        actions={
          <>
            <input
              ref={input}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                const files = Array.from(e.currentTarget.files ?? []);
                e.currentTarget.value = '';
                addFiles(files);
              }}
            />
            <button
              type="button"
              class="act small"
              disabled={!!busy()}
              title="Choose the folder on this computer that holds your samples"
              onClick={() => void choose()}
            >
              Choose folder…
            </button>
            <button
              type="button"
              class="act small"
              disabled={!!busy()}
              title="Add files to the list, from any folder"
              onClick={() => input?.click()}
            >
              Add files…
            </button>
            <span class="drop">or drop files</span>
            <button
              type="button"
              class="act primary"
              disabled={!!busy() || picked().length === 0 || !deviceInfo()}
              title={
                picked().length
                  ? 'Convert, upload and verify the ticked files into the open folder of the device'
                  : 'Tick a file to upload'
              }
              onClick={() => void upload()}
            >
              {busy() === 'Uploading' ? 'Uploading…' : countLabel('Upload', picked().length)}
            </button>
          </>
        }
      >
        <Show when={localFolder()}>
          <Crumbs
            items={localCrumbs(localFolder(), localPath())}
            current={localPath()}
            disabled={!!busy()}
            label="Folder on your computer"
            onOpen={openLocal}
          />
        </Show>
        <Show
          when={lrows().length}
          fallback={
            <p class="empty">
              {localFolder()
                ? 'Nothing here.'
                : 'Choose a folder, or add files. A WAV is converted when it is uploaded.'}
            </p>
          }
        >
          <ul class="rows">
            <For each={lrows()}>
              {(r) => (
                <ListRow
                  check={
                    r.kind === 'file'
                      ? {
                          label: `Upload ${r.name}`,
                          checked: localTicks().has(r.key),
                          disabled: !!busy(),
                          onChange: () => setLocalTicks(flip(localTicks(), r.key)),
                        }
                      : undefined
                  }
                  name={r.name}
                  title={r.added ? `${r.name} (added)` : r.path}
                  open={r.kind === 'folder' ? { disabled: !!busy(), onOpen: () => openLocal(r.path) } : undefined}
                  tags={localRowTags(r)}
                  size={r.kind === 'file' ? formatBytes(r.size) : ''}
                />
              )}
            </For>
          </ul>
        </Show>
      </LocalPane>

      <DevicePane
        ctx={props.ctx}
        selectAll={
          <SelectAll
            label="Select all to delete"
            keys={rows()
              .filter((r) => !r.pending)
              .map((r) => r.path)}
            ticked={sampleTicks()}
            disabled={!!busy()}
            onChange={(next) => {
              setSampleTicks(next);
              confirm.stop();
            }}
          />
        }
        actions={
          <>
            <Show when={deviceInfo()?.canMkdir}>
              <button type="button" class="act" disabled={!!busy()} onClick={() => setNaming(!naming())}>
                New folder
              </button>
            </Show>
            <DeleteButton
              n={nDelete()}
              busy={busy()}
              confirming={confirm.confirming()}
              title="Delete the ticked files and empty folders from the device"
              onClick={() => confirm.click(() => void removeSelected())}
            />
          </>
        }
      >
        <Crumbs
          items={crumbs(sampleFolder())}
          current={sampleFolder()}
          disabled={!!busy()}
          label="Folder on the device"
          onOpen={open}
        />
        <Show when={naming()}>
          <form
            class="newfolder"
            onSubmit={(e) => {
              e.preventDefault();
              createFolder();
            }}
          >
            <input
              type="text"
              aria-label="New folder name"
              placeholder="folder name"
              value={newName()}
              onInput={(e) => setNewName(e.currentTarget.value)}
            />
            <button type="submit" class="act small" disabled={!!busy()}>
              Create
            </button>
          </form>
        </Show>
        <Show when={sampleEntries()} fallback={<p class="empty">{busy() ? `${busy()}…` : 'No listing yet.'}</p>}>
          <Show when={rows().length} fallback={<p class="empty">This folder is empty.</p>}>
            <ul class="rows">
              <For each={rows()}>
                {(r) => (
                  <ListRow
                    check={{
                      label: `Delete ${r.name}`,
                      checked: sampleTicks().has(r.path),
                      disabled: !!busy() || r.pending,
                      onChange: () => toggle(r),
                    }}
                    name={r.name}
                    title={r.path}
                    open={r.kind === 'folder' ? { disabled: !!busy(), onOpen: () => open(r.path) } : undefined}
                    size={r.kind === 'file' ? formatBytes(r.size) : ''}
                    status={r.status}
                  />
                )}
              </For>
            </ul>
          </Show>
        </Show>
        <Show when={deviceInfo() && storeNote(deviceInfo()!.samples, deviceInfo()!.name)}>
          <p class="empty note">{storeNote(deviceInfo()!.samples, deviceInfo()!.name)}</p>
        </Show>
        <Show when={note()}>
          <p class="empty note">{note()}</p>
        </Show>
      </DevicePane>
    </SplitPane>
  );
}
