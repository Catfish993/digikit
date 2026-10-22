// The preview entry: the desktop shell (top bar, sidebar, content) around the real Transfer page, over the fake device of sim.ts.
// Mirrors the markup of ../src/Console.tsx for the parts around the page, so widths and heights are the app's.
import { render } from 'solid-js/web';
import '@fontsource-variable/archivo/wdth.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@web/styles/site.css';
import '../src/console.css';
import '../src/native.css';
import { installNativeFeel } from '../src/native';
import TransferPage from '../src/TransferPage';
import { setLocalFolder, setMode } from '../src/transfer-state';
import { SAMPLES_ROOT, control, sim } from './sim';

installNativeFeel();
setMode(sim.mode);
setLocalFolder(sim.folder === 'none' ? '' : SAMPLES_ROOT);
(window as unknown as { __sim: typeof control }).__sim = control;

render(
  () => (
    <div class="console">
      <header class="topbar">
        <span class="path">
          UI preview: fake device (state in the address: ?dev=ok|empty|noreply|error|slow &amp; folder=files|empty|none
          &amp; speed=MiB/s &amp; mode=modules|samples)
        </span>
      </header>
      <div class="shell">
        <nav class="sidebar" aria-label="Pages">
          <button type="button" class="side toggle" aria-label="Collapse the sidebar">
            <span>«</span>
          </button>
          <button type="button" class="side">
            <span>Emulator</span>
          </button>
          <button type="button" class="side on" aria-current="page">
            <span>Transfer</span>
          </button>
          <button type="button" class="side">
            <span>Remote</span>
          </button>
        </nav>
        <div class="content">
          <TransferPage />
        </div>
      </div>
    </div>
  ),
  document.getElementById('app')!,
);
