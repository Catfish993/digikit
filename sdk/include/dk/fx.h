/* <dk/fx.h>: the effect contract for content modules.
   Hand-written. Layouts come from the generated <dk/gen/fx.h> and <dk/gen/desc.h> (from sdk/sdk.toml).

   Declare each effect in a C file of the module:

     #include <dk/fx.h>
     DK_FX(fshift, .process = "_fs_process", .desc = "_fs_desc",
           .tab = "FSH", .title = "Freq Shifter",
           .route = DK_ROUTE_SEND, .routes = DK_ROUTE_SEND | DK_ROUTE_MASTER,
           .knob = { DK_KNOB(0, "Freq Shift", "SHIFT", DK_BIPOLAR, -64, 63, 0) });

   The record goes to the .dk.desc section. `patchkit pack` reads it; the device never sees it.
   MIX defaults to 127. Text longer than its field is a compile error. */
#ifndef DK_FX_H
#define DK_FX_H
#include <dk/gen/fx.h>
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

/* An effect: ID is a C identifier and becomes the effect id. The rest are dk_desc_fx field initializers. */
#define DK_FX(ID, ...)                                                         \
    DK_DESC_OVERRIDE_BEGIN                                                     \
    static const dk_desc_fx dk_desc_fx_##ID DK_DESC = {                        \
        .kind = DK_DESC_FX, .size = sizeof(dk_desc_fx), .id = #ID, .mix = 127, \
        __VA_ARGS__};                                                          \
    DK_DESC_OVERRIDE_END

/* Knob WORD of an effect, at page position WORD. */
#define DK_KNOB(WORD, NAME, SHORT, DISPLAY, MIN, MAX, DEF) \
    DK_KNOB_AT(WORD, WORD, NAME, SHORT, DISPLAY, MIN, MAX, DEF)

/* Knob WORD of an effect, at page position POS (0 .. 7). */
#define DK_KNOB_AT(WORD, POS, NAME, SHORT, DISPLAY, MIN, MAX, DEF)                   \
    [WORD] = {.word = (WORD), .position = (POS), .display = (DISPLAY), .min = (MIN), \
              .max = (MAX), .def = (DEF), .name = NAME, .short_name = SHORT}

#endif
