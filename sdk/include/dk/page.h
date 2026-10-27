/* <dk/page.h>: what <dk/fx.h> and <dk/machine.h> share. Hand-written.
   Layouts come from the generated <dk/gen/desc.h> (from sdk/sdk.toml [desc]).

   A page of encoders for a machine or an effect:

     DK_PAGE(pluck, "PLUCK", "Pluck", DK_KNOB(0), DK_KNOB(1), DK_KNOB(2), DK_KNOB(3));
     DK_PAGE(fshift, "FSH", "Freq Shifter", DK_KNOB_AT(0, 0));

   The records go to the .dk.desc section. `patchkit` reads them; the device never sees them.
   Text longer than its field is a compile error. */
#ifndef DK_PAGE_H
#define DK_PAGE_H
#include <dk/gen/desc.h>

/* The record section. A host build on macOS (unit tests) needs a Mach-O "segment,section" name. */
#if defined(__APPLE__)
#define DK_DESC __attribute__((section("__DATA,__dk_desc"), used, aligned(4)))
#else
#define DK_DESC __attribute__((section(".dk.desc"), used, aligned(4)))
#endif

#if defined(__clang__)
#define DK_DESC_OVERRIDE_BEGIN \
    _Pragma("clang diagnostic push") _Pragma("clang diagnostic ignored \"-Winitializer-overrides\"")
#define DK_DESC_OVERRIDE_END _Pragma("clang diagnostic pop")
#else
#define DK_DESC_OVERRIDE_BEGIN \
    _Pragma("GCC diagnostic push") _Pragma("GCC diagnostic ignored \"-Woverride-init\"")
#define DK_DESC_OVERRIDE_END _Pragma("GCC diagnostic pop")
#endif

/* Paste two tokens after expanding them (for unique variable names). */
#define DK_CAT_(A, B) A##B
#define DK_CAT(A, B) DK_CAT_(A, B)

/* A page of encoders. OWNER is the machine or effect id. The rest: DK_KNOB / DK_KNOB_AT entries, up to 8. */
#define DK_PAGE(OWNER, NAME, TITLE, ...)                                        \
    static const dk_desc_page DK_CAT(dk_desc_page_##OWNER##_, __LINE__) DK_DESC = { \
        .kind = DK_DESC_PAGE, .size = sizeof(dk_desc_page), .owner = #OWNER,    \
        .name = NAME, .title = TITLE, .knob = {__VA_ARGS__}}

/* The encoder of parameter WORD, at the lowest free position. */
#define DK_KNOB(WORD) {.used = 1, .word = (WORD), .position = DK_POS_AUTO}

/* The encoder of parameter WORD, at position POS (0 .. 7). */
#define DK_KNOB_AT(WORD, POS) {.used = 1, .word = (WORD), .position = (POS)}

#endif
