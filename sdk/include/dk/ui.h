/* <dk/ui.h>: menus, lists and dialogs on the device screen. Hand-written.
   The calls are device-agnostic. core/ui implements them for each device with the firmware's own views.

   A menu is built once and kept. Its fill function adds the rows each time the menu opens or is
   refreshed:

     static void fill(dk_ui_menu *m, void *ctx) {
         dk_ui_action(m, "APPLY", apply, ctx, 0);
         dk_ui_toggle(m, "REMOTE", &remote_on, remote_changed, ctx);
     }
     static const dk_ui_menu_desc main_desc = {.name = "MyMenu", .title = "MY MODULE", .fill = fill};
     dk_ui_open(dk_ui_menu_new(&main_desc));

   Lifetime rules:
   - Text is copied when a row is added or a dialog opens. The caller may reuse its buffers.
   - ctx is kept and passed to the callbacks later, after the opening function has returned.
     It must point at static or module memory, never at the stack.
   - Callbacks run on the UI task. A dialog's callback runs after the dialog has closed.
   - Menus are never freed. Dialogs free themselves after they close.

   Keys: YES runs a row's action, flips a toggle, or (in a list with a check function) toggles the
   row's check mark. The encoder changes a value row; changed is called only when the value moves.
   In a list, RIGHT opens the side panel; choosing a panel row closes the panel, then runs the row.
   LEFT or NO closes the panel. NO on a menu goes back.

   Before a call that a device may not have, check dk_ui_has(cap). */
#ifndef DK_UI_H
#define DK_UI_H
#include <stdint.h>

/* Bytes of row text, terminator included. Longer text is cut. */
#define DK_UI_ROW 24
/* Characters a toast shows. */
#define DK_UI_TOAST 32

typedef struct dk_ui_menu dk_ui_menu;

/* Adds the rows of a menu or of a list's side panel. */
typedef void dk_ui_fill_fn(dk_ui_menu *m, void *ctx);
/* A row action or a change: arg is the value the row was added with (an action) or the new value
   (a toggle or a value). */
typedef void dk_ui_act_fn(void *ctx, int arg);

/* Menu flags. */
enum {
    DK_UI_TEXT_PAGE = 1, /* rows are text to read: no highlighted row, the keys scroll */
    DK_UI_LIST = 2,      /* a dense list with a title bar, a check column and a side panel */
};

typedef struct dk_ui_menu_desc {
    const char *name;   /* view name, for logs */
    const char *title;  /* shown in the title */
    uint32_t flags;     /* DK_UI_* */
    dk_ui_fill_fn *fill;
    void *ctx;
    /* DK_UI_LIST only. Each may be 0. */
    const char *empty;                                    /* text shown when the list has no rows */
    void (*panel)(dk_ui_menu *panel, int row, void *ctx); /* RIGHT on row: add the side-panel rows */
    void (*check)(void *ctx, int row, int on);            /* YES on row toggled its check mark */
    int (*back)(void *ctx);                               /* NO on the list: return 1 to stay open */
} dk_ui_menu_desc;

/* Capabilities for dk_ui_has. */
enum {
    DK_UI_CAP_MENU = 1,
    DK_UI_CAP_TEXT_PAGE,
    DK_UI_CAP_LIST,
    DK_UI_CAP_TOGGLE,
    DK_UI_CAP_VALUE,
    DK_UI_CAP_CONFIRM,
    DK_UI_CAP_CHOICE,
    DK_UI_CAP_TOAST,
};

/* 1 when this device has the capability. A missing one makes its calls do nothing and return -1. */
int dk_ui_has(int cap);

/* Menus and lists. */
dk_ui_menu *dk_ui_menu_new(const dk_ui_menu_desc *d); /* 0 when out of memory; d must stay valid */
int dk_ui_open(dk_ui_menu *m); /* fill, then show on top of the current view; if open, refresh */
void dk_ui_close(dk_ui_menu *m);
int dk_ui_is_open(const dk_ui_menu *m);
void dk_ui_refresh(dk_ui_menu *m); /* fill again; the selection stays on the same row when it can */

/* Rows, added from a fill function. In a list, the row number (from 0, in the order added) is the
   row argument of panel and check. */
void dk_ui_action(dk_ui_menu *m, const char *text, dk_ui_act_fn *fn, void *ctx, int arg);
void dk_ui_submenu(dk_ui_menu *m, const char *text, dk_ui_menu *sub);
void dk_ui_text(dk_ui_menu *m, const char *text); /* no action */
/* ON/OFF. *value is read when drawn; changed gets the new value after *value is written. */
void dk_ui_toggle(dk_ui_menu *m, const char *text, int *value, dk_ui_act_fn *changed, void *ctx);
/* A number from min to max, changed by the encoder. */
void dk_ui_value(dk_ui_menu *m, const char *text, int *value, int min, int max, dk_ui_act_fn *changed,
                 void *ctx);

/* The check marks of a DK_UI_LIST. */
void dk_ui_check(dk_ui_menu *list, int row, int on);
int dk_ui_checked(const dk_ui_menu *list, int row);

/* Dialogs. Each returns 0 when shown, -1 when not (missing capability, out of memory, or another
   dialog is open). */
/* YES or NO. done gets 1 for YES, 0 for NO. title and text: up to 80 characters each. */
int dk_ui_confirm(const char *title, const char *text, void (*done)(void *ctx, int yes), void *ctx);
/* One of n items. done gets the index; NO closes the dialog without calling done. */
int dk_ui_choice(const char *title, const char *const *items, int n, void (*done)(void *ctx, int index),
                 void *ctx);
/* A short message on the main screen that goes away by itself. */
int dk_ui_toast(const char *text);

#endif
