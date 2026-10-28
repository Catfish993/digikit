# test/ui-demo

SETTINGS > DIGIKIT opens a menu with one row of each kind of core/ui: a toggle, a value, a toast, a confirm, a choice, a text page, a list
(check marks, a side panel with two actions) and an empty list. It conflicts with core/digikit-menu (the same hook). Driven by
`uv run python -m tools.patchkit.pk_uidemo shots|leak --device dt2|dn2`.
