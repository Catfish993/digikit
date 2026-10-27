# core/ui

The device adapter behind `<dk/ui.h>` (`sdk/include/dk/ui.h`): menus, text pages, lists with a title bar, check marks and a side panel, rows
(action, submenu, text, toggle, value), confirm and choice dialogs and toasts. C, one source for the Digitone II and the Digitakt II; every firmware
call is a generated anchor (`modules/core/fw/include/dk_fw_*.h`). The host fake of the same API, with its tests, is in `host/`.

- A plain menu and a text page are the firmware's `VerticalMenuView`; a `DK_UI_LIST` is a `MenuWithPopoutSideBarsView` (the PRESET MANAGER's
  base) with the right panel as the side panel. Each is built once and kept for the session through a `shared_ptr` this module owns, so the
  firmware never frees it.
- Rows are stock `MenuItem`s. Their `std::function`s keep two words (the menu and the row number) in the function's own storage: no heap
  per row. The row data lives in an array of the menu that a refill rewrites.
- The View vtable of a text page and of a list is a copy in module RAM: paint (slot 4) hides the cursor bar of a text page and draws the
  empty text of a list; key (slot 2) of a list adds RIGHT (side panel), YES (check mark) and the `back` callback, and sends the rest to the
  stock handler.
- Dialogs are `make_shared` blocks built by hand and pushed on the Brain's view manager; the firmware frees them. One at a time.
- `dk_ui_stock_row(stock_menu, text, fn, ctx)` is for other core modules: a row among the rows of a stock menu (SETTINGS), with its icon.

Proof and demo: `tests/fixtures/modules/ui-demo`, driven by `uv run python -m tools.patchkit.pk_uidemo shots|leak --device dt2|dn2`.
Details: [docs/findings/71-ui-components-1-17.md](../../../docs/findings/71-ui-components-1-17.md).
