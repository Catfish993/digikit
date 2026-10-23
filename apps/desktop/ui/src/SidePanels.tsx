// The right-hand column of the Emulator page and the Remote page: the Card panel on top (the Emulator page only) and the Memory panel under it,
// each collapsible. A closed panel keeps its title bar; when nothing in the column is open it shrinks to a narrow rail, so the faceplate
// takes the width back. Both panels read through the one session of their target (target.ts).
import { Show, type JSX } from 'solid-js';
import type { DkSession } from '@web/modules/dkupload-core';
import CardPanel from './CardPanel';
import MemoryPanel from './MemoryPanel';
import { cardOpen, memoryOpen } from './panel-open';
import './card-panel.css';

/**
 * `visible`: the page is shown (and, for the Emulator page, the emulator runs). `card`: show the Card panel too, with its `reload`.
 * `session`, `where`, `track`: the Memory panel's (memory-panel props).
 */
export default function SidePanels(props: {
  visible: boolean;
  card?: boolean;
  reload?: () => Promise<void>;
  session: () => Promise<DkSession>;
  where: () => string;
  track?: () => unknown;
}): JSX.Element {
  const rail = () => !(props.card && cardOpen()) && !memoryOpen();
  return (
    <div class="side-col" classList={{ rail: rail() }} hidden={!props.visible}>
      <Show when={props.card}>
        <CardPanel visible={props.visible} reload={props.reload} />
      </Show>
      <MemoryPanel visible={props.visible} session={props.session} where={props.where} track={props.track} />
    </div>
  );
}
