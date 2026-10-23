// Whether the Card and Memory panels are open (the right-hand column of the Emulator page, SidePanels.tsx). The choice is kept in localStorage per
// panel; SidePanels reads both to shrink the column to a rail when nothing in it is open.
import { createSignal, type Accessor } from 'solid-js';

/** Below this window width the Memory panel starts closed, unless the user chose (the column would take a quarter of the window). */
export const WIDE_PX = 1100;

function persisted(key: string, initial: () => boolean): [Accessor<boolean>, (open: boolean) => void] {
  const read = () => {
    try {
      const saved = localStorage.getItem(key);
      if (saved) return saved !== 'closed';
    } catch {
      /* no choice kept */
    }
    return initial();
  };
  const [open, set] = createSignal(read());
  return [
    open,
    (next) => {
      set(next);
      try {
        localStorage.setItem(key, next ? 'open' : 'closed');
      } catch {
        /* the choice lasts until the window closes */
      }
    },
  ];
}

export const [cardOpen, setCardOpen] = persisted('digikit.cardPanel', () => true);
export const [memoryOpen, setMemoryOpen] = persisted('digikit.memoryPanel', () => window.innerWidth >= WIDE_PX);
