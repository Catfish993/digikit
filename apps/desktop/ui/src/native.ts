// The behaviour half of the native feel (native.css is the look): the browser's context menu only where copying is useful, and nothing
// dragged out of the window. Dropping files onto the window is not touched: it does not start a drag here and `drop` is not listened to.
import { contextMenuAllowed, dragAllowed } from './native-core';

export function installNativeFeel(doc: Document = document): void {
  doc.addEventListener('contextmenu', (e) => {
    if (!contextMenuAllowed(e.target as Element | null)) e.preventDefault();
  });
  doc.addEventListener('dragstart', (e) => {
    if (!dragAllowed(e.target as Element | null)) e.preventDefault();
  });
}
