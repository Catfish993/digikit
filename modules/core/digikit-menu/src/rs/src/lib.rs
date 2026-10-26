//! core/digikit-menu: SETTINGS > DIGIKIT (docs/findings/28-digikit-menu.md).
//!
//! Built with rustc_codegen_gcc for the ColdFire (finding 26). The firmware's own menu classes are used as they
//! are (finding 23, S2): a `MenuItem` built by the firmware's builder, `VerticalMenuView` objects, `std::function`
//! objects with our own manager and invoker, and the view stack push. Every firmware function is declared by the
//! generated crate `dkfw` (devices/fw/): its symbol is the anchor name, and nothing here knows an address.
//!
//! Boundary rules (finding 23, S1b): integers and pointers only; the firmware calls us with the gcc m68k cdecl
//! convention (arguments on the stack, result in d0), which Rust's `extern "C"` matches.
#![no_std]
#![allow(static_mut_refs, dangerous_implicit_autorefs)]

use core::mem::{offset_of, size_of, transmute};
use core::panic::PanicInfo;
use core::ptr::{addr_of, addr_of_mut, null, null_mut};
use dkfw::menu::{MenuItem, VerticalMenuView};
use dkfw::string::{shared_ptr, std_function};
use dkfw::{mem, menu, string, view};
use dkneeds::{needs_of, Bytes};
use dkrt_fit::{self as fit, Needs, Short};
use dkrt_sdk::proto::{
    CAPK_ARENA, CAPK_DSP_AREA, CAPK_MACHINE_ROWS, CAPK_MODULES, CAPK_PAGE_ROWS, CAPK_PARAM_ROWS, CAPK_SEND_SLOTS, CAPK_STAGE, CAPK_VOICE_ROWS,
};
use dkrt_sdk::{DkrtFx, DkrtStoreEntry, DkrtBootInfo, DkrtCap, CAP_MAX, DkrtDiag, DkrtHealth, CARD_FOREIGN, CARD_UNREADABLE, DkrtModule, HEALTH_CPU, HEALTH_DSP, HEALTH_HEAP, HEALTH_HEAP_CUT, HEALTH_MODULE, HEALTH_STORE, DkrtSelection, DkrtStatus, E_NOT_FOUND, E_READ, OK, SAFE_COUNTER, SAFE_FLAG, SAFE_KEY, SEL_MAX, SEL_PATH};

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

// ---- firmware functions (dkfw) and the assembly shims ---------------------------------------------------------

// The firmware functions are `dkfw::{mem, menu, string, view}` (the declarations and their notes are in devices/fw/). The popup's
// pointer plumbing is as `FUN_400a4832` ("LOAD ERROR") does it: move the pointer into a `shared_ptr<View>`, the Brain singleton,
// its view manager (Brain + 0x40), add the view (exclusive flag 0), release a count holder.
extern "C" {
    /// Assembly shim (shim.s): `shared_ptr<View>` from a raw `View*` with the opener as parent (A0 = out).
    fn dk_make_sp(out: *mut SharedPtr, parent: *mut u8, view: *mut u8);
    /// Assembly shim (shim.s): `make_shared<PopupWindow>(const char *const &)` into `out` (A0 = out); `text` points at the
    /// pointer to the NUL-terminated message.
    fn dk_popup_make(out: *mut SharedPtr, text: *const *const u8);
}

/// The View sub-object of a `VerticalMenuView`, its cursor row (a Menu field) and the View's manager pointer, which
/// `View::invalidate` tests (0 when the view is not on the stack).
const VIEW_AT: usize = offset_of!(VerticalMenuView, view);
const CURSOR_AT: usize = offset_of!(VerticalMenuView, cursor);
const VIEW_MGR_AT: usize = VIEW_AT + offset_of!(dkfw::view::View, mgr);
/// The `draw` function of a `MenuItem` (a `std::function`): storage, then the manager.
const DRAW_STORAGE_AT: usize = offset_of!(MenuItem, draw);
const DRAW_MANAGER_AT: usize = DRAW_STORAGE_AT + offset_of!(std_function, manager);

// ---- the runtime (core/runtime) --------------------------------------------------------------------------------

// The menu `requires` core/runtime, so these are plain symbols the linker resolves to the runtime's exports. The layouts
// (`DkrtStatus`, `DkrtModule`) and the result codes are the runtime's own, from the shared crate `dkrt-sdk`.
extern "C" {
    /// The saved selection (what the next boot loads): `count` store paths. Always 0.
    fn dkrt_selection_get(out: *mut DkrtSelection) -> i32;
    /// Save a selection (two copies in the card's store range); 0 or an `E_SEL_*` code. No effect until the next boot.
    fn dkrt_selection_set(sel: *const DkrtSelection) -> i32;
    /// Soft reset. Returns only when it refuses (`E_CARD_BUSY`, ...).
    fn dkrt_restart() -> i32;
    /// This boot: safe mode and why, the modules selected and each one's result.
    fn dkrt_boot_info(out: *mut DkrtBootInfo);
    fn dkrt_status(out: *mut DkrtStatus);
    /// The `index`th loaded module; 0, or `E_NOT_FOUND` past the end.
    fn dkrt_module(index: u32, out: *mut DkrtModule) -> i32;
    /// CPU load, the DSP's block time, the heap, the arena and the DSP area (finding 41).
    fn dkrt_health(out: *mut DkrtHealth);
    /// The CAPACITY command's entries (limit, used, largest per resource); how many were written, at most `max`.
    fn dkrt_capacity(out: *mut DkrtCap, max: u32) -> u32;
    /// DSP blocks since boot (a 1500 Hz clock), 0 without core/health.
    fn dkrt_blocks() -> u32;
    /// A function the UI loop calls once per pass (0 removes it).
    fn dkrt_poll_set(f: usize);
    /// The card check, the debug log, the DSP watch and the crash record (finding 46).
    fn dkrt_diag(out: *mut DkrtDiag);
    /// The debug log keeps every event (1) or errors only (0); saved in the selection record. 0 or an `E_*` code (`E_CARD_RO`: held until the next boot).
    fn dkrt_debug_set(on: u32) -> i32;
    /// The files of the module store whose path starts with `prefix` (NUL-terminated), sorted by path, at most `max` into `out`: the count, or a negative code
    /// (`E_NOT_FOUND`: no store).
    fn dkrt_store_list(prefix: *const u8, out: *mut DkrtStoreEntry, max: u32) -> i32;
    /// One sector of the store (relative to its start) into `out` (512 bytes, 16-byte aligned).
    fn dkrt_store_read_sector(rel: u32, out: *mut u8) -> i32;
    /// The REMOTE switch (the commands of the remote range are answered only while it is on; not saved): 1 on, 0 off, -1 when the image has no input
    /// adapter (core/input) and so no remote control.
    fn dkrt_remote_get() -> i32;
    fn dkrt_remote_set(on: u32) -> i32;
    /// The `index`th loaded effect that has a page: tab, module id, allowed routes, default, the route it runs on and the route the settings give it; 0, or `E_NOT_FOUND` past the end.
    fn dkrt_fx(index: u32, out: *mut DkrtFx) -> i32;
    /// digikit's settings (finding 63): set `key` (NUL-terminated) to `len` bytes; written to the card 2 s later, or at once by the flush. 0 or an `E_SET_*` code.
    fn dkrt_settings_put(key: *const u8, value: *const u8, len: u32) -> i32;
    fn dkrt_settings_flush() -> i32;
}

// ---- std::function and shared_ptr as the firmware lays them out -----------------------------------------------

/// libstdc++ `std::function<void()>`: 8 bytes of functor storage, manager at +8 (0 = empty), invoker at +0xc.
/// Our functor is two words kept in the storage: the action code and a context pointer.
#[repr(C)]
pub struct StdFn {
    d0: u32,
    d1: u32,
    mgr: usize,
    inv: usize,
}
const EMPTY_FN: StdFn = StdFn { d0: 0, d1: 0, mgr: 0, inv: 0 };

/// `std::shared_ptr<View>`: pointer and control block (the layout of `dkfw::string::shared_ptr`).
pub type SharedPtr = shared_ptr;

static TYPEINFO_DUMMY: u32 = 0;

/// `_M_manager(dest, src, op)`: 0 type info, 1 functor pointer, 2 clone, 3 destroy. The functor lives in the
/// storage, so clone copies two words and destroy does nothing.
unsafe extern "C" fn fn_manager(dest: *mut u32, src: *const u32, op: u32) -> u32 {
    match op {
        0 => *dest = addr_of!(TYPEINFO_DUMMY) as u32,
        1 => *dest = src as u32,
        2 => {
            *dest = *src;
            *dest.add(1) = *src.add(1);
        }
        _ => {}
    }
    0
}

/// `_M_invoke(const _Any_data&)`.
unsafe extern "C" fn fn_invoke(d: *const u32) {
    dispatch(*d, *d.add(1));
}

fn make_fn(act: u32, ctx: u32) -> StdFn {
    StdFn { d0: act, d1: ctx, mgr: fn_manager as usize, inv: fn_invoke as usize }
}

// ---- views -------------------------------------------------------------------------------------------------------

#[repr(C)]
struct View {
    obj: *mut u8, // the VerticalMenuView object (what `this` is for the firmware's menu code)
    sp: *mut SharedPtr, // in the firmware heap: the firmware's shared_ptr code writes it, and module RAM stays write-free
}
const NO_VIEW: View = View { obj: null_mut(), sp: null_mut() };

static mut MAIN: View = NO_VIEW;
static mut LOADV: View = NO_VIEW;
static mut ABOUTV: View = NO_VIEW;
static mut HEALTHV: View = NO_VIEW;
static mut MEMV: View = NO_VIEW;
static mut FXRV: View = NO_VIEW;

static NAME_MAIN: &[u8] = b"DigikitMenu\0";
static NAME_LOAD: &[u8] = b"DigikitLoadMenu\0";
static NAME_ABOUT: &[u8] = b"DigikitAboutMenu\0";
static NAME_HEALTH: &[u8] = b"DigikitHealthMenu\0";
static NAME_MEM: &[u8] = b"DigikitMemoryMenu\0";
static NAME_FXR: &[u8] = b"DigikitFxRoutesMenu\0";

/// The address of the stock icon the SYSTEM item uses, read from the operand of the instruction that pushes it.
unsafe fn icon() -> *const u8 {
    let r = addr_of!(menu::ICON_REF) as *const u16;
    (((*r.add(1) as u32) << 16) | *r.add(2) as u32) as *const u8
}

unsafe fn vslot(obj: *mut u8, off: usize) -> usize {
    *((*(obj as *const *const u8)).add(off) as *const usize)
}

/// Menu::addItem (slot +8): the view takes the raw item.
unsafe fn add_item(menu: *mut u8, item: *mut u8) {
    let f: unsafe extern "C" fn(*mut u8, *mut u8) = transmute(vslot(menu, 8));
    f(menu, item);
}

/// The firmware's rebuild idiom: selection, clear (slot +0x14), add the items, restore the selection (slot +0x1c).
unsafe fn begin_rebuild(menu: *mut u8) -> u32 {
    let sel: unsafe extern "C" fn(*mut u8) -> u32 = transmute(vslot(menu, 0x24));
    let clear: unsafe extern "C" fn(*mut u8) = transmute(vslot(menu, 0x14));
    let s = sel(menu);
    clear(menu);
    s
}
unsafe fn end_rebuild(menu: *mut u8, sel: u32) {
    let restore: unsafe extern "C" fn(*mut u8, u32, u32) = transmute(vslot(menu, 0x1c));
    restore(menu, sel, 2);
    // an action that changes the rows does not move the selection, and the view manager redraws only a view that was
    // invalidated: without this the screen shows the old rows until the next key
    view::invalidate(menu.add(VIEW_AT));
}

/// One MenuItem with a text label and an action; returned to the caller, which hands it to `add_item`. `glyph`: keep the stock
/// icon the builder always gives an item (the DIGIKIT row among the stock SETTINGS rows, which all have one); without it the
/// icon function is destroyed and emptied after the build, as the firmware's own glyph-less items have it (the MenuItem
/// constructor copies the icon function only when its manager, +0x34, is not 0; the row's paint skips an empty one).
unsafe fn make_item(label: *const u8, act: u32, ctx: u32, glyph: bool) -> *mut u8 {
    let item = mem::new(size_of::<MenuItem>() as u32);
    let mut action = make_fn(act, ctx);
    let mut fn4 = EMPTY_FN;
    menu::item_builder(item, label, &action as *const StdFn as *const std_function, icon(), &fn4 as *const StdFn as *const std_function);
    // the builder copied both; destroy the temporaries as the firmware does (fn4 first)
    fn_manager(addr_of_mut!(fn4) as *mut u32, addr_of!(fn4) as *const u32, 3);
    fn_manager(addr_of_mut!(action) as *mut u32, addr_of!(action) as *const u32, 3);
    if !glyph {
        // MenuItem: icon std::function at +0x2c (storage), +0x34 (manager), +0x38 (invoker); op 3 frees the copied bitmap
        let storage = item.add(DRAW_STORAGE_AT) as *mut u32;
        let manager = item.add(DRAW_MANAGER_AT) as *mut usize;
        if *manager != 0 {
            let destroy: unsafe extern "C" fn(*mut u32, *const u32, u32) -> u32 = transmute(*manager);
            destroy(storage, storage, 3);
            *manager = 0;
        }
    }
    item
}

unsafe fn row(menu: *mut u8, text: &[u8], act: u32, ctx: u32) {
    add_item(menu, make_item(text.as_ptr(), act, ctx, false));
}

/// Build a stock VerticalMenuView once; `parent` is the object that will push it.
unsafe fn ensure_view(v: *mut View, name: &'static [u8], title: &'static [u8], parent: *mut u8) {
    if !(*v).obj.is_null() {
        return;
    }
    let obj = mem::new(size_of::<VerticalMenuView>() as u32);
    let mut s: u32 = 0;
    let alloc: u8 = 0;
    string::ctor(&mut s, title.as_ptr(), &alloc);
    // no icon: the view draws one at the bottom left only when the pointer (`VerticalMenuView::icon`) is not null (`TotalRecallMenuView` passes null)
    menu::vertical_menu_view_ctor(obj, name.as_ptr(), 8, &s, null());
    string::dtor(&mut s);
    (*v).obj = obj;
    // the View sub-object sits at `VIEW_AT`
    (*v).sp = mem::new(size_of::<SharedPtr>() as u32) as *mut SharedPtr;
    dk_make_sp((*v).sp, parent, obj.add(VIEW_AT));
}

// ---- text pages: no highlighted row ------------------------------------------------------------------------------------
//
// ABOUT, HEALTH and MEMORY show text, not choices. The stock paint (`VerticalMenuView::vfunc_31`) inverts the row at the Menu's
// cursor (`this+0x18`, a row index in the visible window) and nothing else decides it. The View sub-object (+0x38) reaches the paint
// through slot 4 of its vtable, a thunk that subtracts 0x38 and branches to it. These pages get a copy of that vtable (17 slots,
// held in module RAM) whose slot 4 sets the cursor out of range around the stock paint: no row matches, the cursor and the
// selection themselves are untouched, so the keys scroll a long page as before.

/// The View vtable of a stock `VerticalMenuView`: 17 slots, preceded by the two words the C++ runtime reads at negative offsets
/// (`offset_to_top`, the type info: the view manager casts through them), so the copy has 19 words and the pointer is at word 2.
const VIEW_VT_SLOTS: usize = 17;
static mut TEXT_VT: [usize; VIEW_VT_SLOTS + 2] = [0; VIEW_VT_SLOTS + 2];
/// The stock slot 4 (the thunk to the paint).
static mut TEXT_PAINT: usize = 0;

unsafe extern "C" fn text_paint(view: *mut u8, canvas: *mut u8) {
    let cursor = view.sub(VIEW_AT).add(CURSOR_AT) as *mut u32;
    let saved = *cursor;
    *cursor = 0x7fff_ffff;
    let paint: unsafe extern "C" fn(*mut u8, *mut u8) = transmute(TEXT_PAINT);
    paint(view, canvas);
    *cursor = saved;
}

/// Give the page `obj` (a `VerticalMenuView` built by `ensure_view`) the text paint. It leaves the page as it is when the stock slot is
/// not the expected thunk (`moveq #-0x38,d0` is 0x70c8): rows with the cursor bar, as before.
unsafe fn text_page(obj: *mut u8) {
    let vptr = obj.add(VIEW_AT) as *mut usize;
    let new = addr_of_mut!(TEXT_VT[2]) as usize;
    if *vptr == new {
        return;
    }
    let old = *vptr as *const usize;
    if TEXT_PAINT == 0 {
        if *(*old.add(4) as *const u16) != 0x70c8 {
            return;
        }
        let mut i = 0;
        while i < VIEW_VT_SLOTS + 2 {
            TEXT_VT[i] = *old.sub(2).add(i);
            i += 1;
        }
        TEXT_PAINT = TEXT_VT[2 + 4];
        TEXT_VT[2 + 4] = text_paint as usize;
    }
    *vptr = new;
}

unsafe fn push(from: *mut u8, v: *mut View) {
    view::push_ctx(from, (*v).sp);
}

// ---- small text helpers ------------------------------------------------------------------------------------------

#[repr(C)]
pub struct Text {
    b: [u8; TEXT],
    n: usize,
}
const TEXT: usize = 48;
impl Text {
    const fn new() -> Text {
        Text { b: [0; TEXT], n: 0 }
    }
    fn clear(&mut self) {
        self.b = [0; TEXT];
        self.n = 0;
    }
    /// Spaces up to `n` characters.
    fn pad(&mut self, n: usize) -> &mut Text {
        while self.n < n && self.n < TEXT - 1 {
            self.b[self.n] = b' ';
            self.n += 1;
        }
        self
    }
    fn s(&mut self, t: &[u8]) -> &mut Text {
        for &c in t {
            if self.n < TEXT - 1 && c != 0 {
                self.b[self.n] = c;
                self.n += 1;
            }
        }
        self
    }
    fn num(&mut self, mut v: u32) -> &mut Text {
        let mut tmp = [0u8; 10];
        let mut k = 0;
        loop {
            tmp[k] = b'0' + (v % 10) as u8;
            v /= 10;
            k += 1;
            if v == 0 {
                break;
            }
        }
        while k > 0 {
            k -= 1;
            self.s(&tmp[k..k + 1]);
        }
        self
    }
    fn hex(&mut self, v: u32) -> &mut Text {
        let mut k = 8;
        while k > 0 {
            k -= 1;
            let d = ((v >> (4 * k)) & 15) as u8;
            self.s(&[if d < 10 { b'0' + d } else { b'A' + d - 10 }]);
        }
        self
    }
    fn int(&mut self, v: i32) -> &mut Text {
        if v < 0 {
            self.s(b"-");
        }
        self.num(v.unsigned_abs())
    }
    fn ptr(&self) -> *const u8 {
        self.b.as_ptr() // zero filled: always terminated
    }
}

// ---- the module store (docs/design/digikit-store.md; the runtime reads it: `dkrt_store_list`, `dkrt_store_read_sector`) -------------------

const MAX_ENT: usize = 32;
/// The store files under `modules/` as the runtime listed them (path, first sector, size), in path order.
static mut ENTRIES: [DkrtStoreEntry; MAX_ENT] = unsafe { core::mem::zeroed() };

/// 0 = not read, 1 = ok, 2 = no store, 3 = read error, 4 = bad or too big
static mut STORE: u32 = 0;
/// Indices into `ENTRIES` of the `.dkm` files.
static mut ENT: [u8; MAX_ENT] = [0; MAX_ENT];
/// The pending selection: 1 when the row is ticked. It starts as the saved selection and is only saved by APPLY & RESTART.
static mut TICK: [u8; MAX_ENT] = [0; MAX_ENT];
/// 1 when the saved selection (`dkrt_selection_get`) holds the row's file.
static mut SAVED: [u8; MAX_ENT] = [0; MAX_ENT];
/// Paths of the saved selection that no row has (the file is gone from the store): APPLY would drop them.
static mut SAVED_EXTRA: u32 = 0;
/// 1 when the runtime has the entry's file loaded in this boot (2: still loading its DSP part).
static mut LOADED: [u8; MAX_ENT] = [0; MAX_ENT];
static mut NENT: usize = 0;
/// 1 when the file's header says this runtime or device cannot load it (shown greyed out, `[-]`, and cannot be ticked).
static mut NOK: [u8; MAX_ENT] = [0; MAX_ENT];
/// The needs of each file, inferred from its contents (`read_needs`), read once per scan; 1 in `HAS_NEEDS` when it has them (a file that is not a readable `.dkm` has none).
static mut NEEDS: [Needs; MAX_ENT] = [NO_NEEDS; MAX_ENT];
static mut HAS_NEEDS: [u8; MAX_ENT] = [0; MAX_ENT];
const NO_NEEDS: Needs = Needs { file_size: 0, arena: 0, arena_xfer: 0, dsp_area: 0, dsp_align: 0, machine_rows: 0, voice_rows: 0, param_rows: 0, page_rows: 0, fx_slots: 0 };
/// 1 when the row's module does not fit next to the ticked ones (`refit`), and the first short resource.
static mut WONT: [u8; MAX_ENT] = [0; MAX_ENT];
static mut SHORT: [Short; MAX_ENT] = [Short { kind: 0, need: 0, free: 0 }; MAX_ENT];
/// The load line of the ticked set (`refit`): the DSP and the CPU memory, `DSP 85% CPU 1%`; empty when nothing is ticked.
static mut LOAD_LINE: Text = Text::new();
/// One 512-byte, 16-byte aligned firmware heap block the card DMA fills with a file's header sector.
static mut HDR: *mut u8 = null_mut();
/// The saved selection as the runtime gave it, and this boot's info (safe mode, results).
static mut SEL: DkrtSelection = DkrtSelection { count: 0, flags: 0, paths: [[0; SEL_PATH]; SEL_MAX] };
static mut BOOT: DkrtBootInfo = unsafe { core::mem::zeroed() };
/// The message row below the buttons (what the last action did), empty: none.
static mut RESULT: Text = Text::new();
/// The text of the last popup (the message the firmware's popup window was given) and how many were shown. Read by the proof.
#[no_mangle]
pub static mut DK_POPUP: Text = Text::new();
#[no_mangle]
pub static mut DK_POPUPS: u32 = 0;
/// 1 after the first press of APPLY & RESTART (the second press restarts); any other action clears it.
static mut ARMED: u32 = 0;
/// 1 once the safe-mode popup was shown in this boot.
static mut SAFE_SHOWN: u32 = 0;
/// What the last APPLY & RESTART did: the result of `dkrt_selection_set` and of `dkrt_restart` (which only returns when it
/// refuses). Read by the proof.
#[no_mangle]
pub static mut DK_APPLY_SET: i32 = 0;
#[no_mangle]
pub static mut DK_APPLY_RESTART: i32 = 0;
#[no_mangle]
pub static mut DK_APPLIES: u32 = 0;
/// 1 when the list the view shows has the APPLY & RESTART row. Read by the proof.
#[no_mangle]
pub static mut DK_OFFER: u32 = 0;

#[repr(C)]
struct ModBuf {
    m: DkrtModule,
    ext: [u32; 8],
}
const MOD_STATE_OFF: usize = 112;
const MOD_LOADING: u32 = 1;

unsafe fn entry_path(e: usize) -> &'static [u8] {
    let p = &ENTRIES[e].path;
    let mut n = 0;
    while n < 63 && p[n] != 0 {
        n += 1;
    }
    &p[..n]
}

/// Ask the runtime which modules it holds and match their store paths against the rows.
unsafe fn refresh_loaded() {
    LOADED = [0; MAX_ENT];
    let mut index = 0;
    loop {
        let mut m: ModBuf = core::mem::zeroed();
        if dkrt_module(index, &mut m.m) != OK {
            break;
        }
        index += 1;
        let mstate = *(&m as *const ModBuf as *const u8).add(MOD_STATE_OFF).cast::<u32>();
        let mut n = 0;
        while n < 63 && m.m.path[n] != 0 {
            n += 1;
        }
        for i in 0..NENT {
            if entry_path(ENT[i] as usize) == &m.m.path[..n] {
                LOADED[i] = if mstate == MOD_LOADING { 2 } else { 1 };
            }
        }
    }
}

const DKM_MAGIC: u32 = 0x444b_4d46; // "DKMF"
const REG_MAGIC: u32 = 0x444b_5247; // "DKRG"
/// A header word at byte `off` of the file starting at store sector `first`, read through the block reader; None on an error.
unsafe fn file_word(first: u32, off: u32) -> Option<u32> {
    if dkrt_store_read_sector(first + off / 512, HDR) != OK {
        return None;
    }
    let p = HDR.add((off % 512) as usize);
    Some(((*p as u32) << 24) | ((*p.add(1) as u32) << 16) | ((*p.add(2) as u32) << 8) | (*p.add(3) as u32))
}

/// Whether the file at store sector `first` can be loaded by this runtime on this device, judged from its header: the `.dkm`
/// format's SDK contracts (header 0x38..0x3f, the loader's rule: a declared contract with another major or a minor above the
/// runtime's refuses), and the device of its register records (docs/design/digikit-dkm.md; a module without records runs on
/// either device). A file that is not a `.dkm` or cannot be read passes: the runtime then names the real reason at boot.
unsafe fn compatible(first: u32, device: u32) -> bool {
    match file_word(first, 0) {
        Some(DKM_MAGIC) => {}
        _ => return true,
    }
    let rt = [
        dkrt_sdk::contract::API,
        dkrt_sdk::contract::MACHINE,
        dkrt_sdk::contract::FX,
        dkrt_sdk::contract::SERVICE,
    ];
    for half in 0..2u32 {
        if let Some(w) = file_word(first, 0x38 + 4 * half) {
            for k in 0..2usize {
                let c = 2 * half as usize + k;
                let major = (w >> (24 - 16 * k as u32)) as u8;
                let minor = (w >> (16 - 16 * k as u32)) as u8;
                if major != 0 && (major != rt[c].0 || minor > rt[c].1) {
                    return false;
                }
            }
        }
    }
    let (Some(off), Some(size)) = (file_word(first, 0x90), file_word(first, 0x94)) else {
        return true;
    };
    if size == 0 || off % 16 != 0 {
        return true;
    }
    if file_word(first, off) != Some(REG_MAGIC) {
        return true;
    }
    match file_word(first, off + 8) {
        Some(d) => d == device || device == 0,
        None => true,
    }
}

/// The sector reads the last scan made to read the needs (the cost of opening the page; read by the proof).
#[no_mangle]
pub static mut DK_NEEDS_READS: u32 = 0;

/// A card file as the needs reader's byte source: the store's sector reads through the block reader, one sector cached (the file is never staged).
struct CardFile {
    first: u32,
    size: u32,
    /// the file's sector held in `HDR`
    held: u32,
}

impl Bytes for CardFile {
    fn size(&self) -> u32 {
        self.size
    }
    fn read(&mut self, at: u32, out: &mut [u8]) -> bool {
        if at > self.size || out.len() as u32 > self.size - at {
            return false;
        }
        let mut i = 0usize;
        while i < out.len() {
            let pos = at + i as u32;
            let sector = pos / 512;
            if self.held != sector {
                // `held` is not trusted across a failed read: the block reader may have written part of the buffer
                self.held = u32::MAX;
                unsafe { DK_NEEDS_READS += 1 };
                if unsafe { dkrt_store_read_sector(self.first + sector, HDR) } != OK {
                    return false;
                }
                self.held = sector;
            }
            out[i] = unsafe { *HDR.add((pos % 512) as usize) };
            i += 1;
        }
        true
    }
}

/// The needs of the `.dkm` at store sector `first` (`size` bytes), inferred from its contents (`dkneeds`, the code patchkit runs): the header, the
/// `register` records and the DSP section's header and voice table, read by sector. None for a file that is not a readable `.dkm` of this format: it
/// has no verdict here and the runtime names the reason at boot.
unsafe fn read_needs(first: u32, size: u32) -> Option<Needs> {
    let mut f = CardFile { first, size, held: u32::MAX };
    needs_of(&mut f).ok()
}

/// Which modules fit. A module set is fixed per session, so the ticked set loads at the next boot into an empty runtime: start from the limits (CAPACITY's
/// limit of each resource, nothing used) and take in the ticked modules in list order; a ticked one that no longer fits is flagged and not taken.
/// Then each unticked module is checked against what the ticked ones leave.
unsafe fn refit() {
    WONT = [0; MAX_ENT];
    let mut caps = [DkrtCap { kind: 0, limit: 0, used: 0, largest: 0 }; CAP_MAX];
    let n = dkrt_capacity(caps.as_mut_ptr(), CAP_MAX as u32) as usize;
    let caps = &mut caps[..n];
    fit::empty(caps);
    LOAD_LINE.clear();
    for pass in 0..2 {
        if pass == 1 {
            // the ticked modules that fit are taken: the DSP and CPU memory of the limits they leave
            let mut any = false;
            for i in 0..NENT {
                any |= TICK[i] != 0;
            }
            if any {
                let mut buf = [0u8; fit::LINE_MAX];
                let n = fit::load_line(caps, &mut buf);
                LOAD_LINE.s(&buf[..n]);
            }
        }
        for i in 0..NENT {
            if HAS_NEEDS[i] == 0 || NOK[i] != 0 || (TICK[i] != 0) != (pass == 0) {
                continue;
            }
            match fit::fits_now(&NEEDS[i], caps) {
                None => {
                    if pass == 0 {
                        fit::take(&NEEDS[i], caps);
                    }
                }
                Some(s) => {
                    WONT[i] = 1;
                    SHORT[i] = s;
                }
            }
        }
    }
}

/// `n` bytes in K for a row: `<1K`, `259K`, `1.2M`.
fn size_k(t: &mut Text, n: u32) {
    if n < 1024 {
        t.s(b"<1K");
    } else if n.div_ceil(1024) < 1000 {
        t.num(n.div_ceil(1024)).s(b"K");
    } else {
        let tenth = (n / 1024 * 10) / 1024;
        t.num(tenth / 10).s(b".").num(tenth % 10).s(b"M");
    }
}

/// The names of the CAPACITY kinds a fit can be short of, by kind number (0 and the kinds that cannot be short: empty).
const SHORT_NAME: [&str; 13] = ["", "MODULES", "SELECTED", "CPU", "FILE", "DSP", "MACHINES", "VOICES", "PARAMS", "PAGES", "FX", "", ""];

/// What is short, in K for bytes and as numbers for rows and slots (the need rounded up, what is free rounded down): `DSP 400K > 254K FREE`, in a
/// popup (28 characters fit with `NO FIT: `); `compact` for the list's RESULT row (about 18 characters fit): `DSP 400K>254K`.
fn short_text(t: &mut Text, s: &Short, compact: bool) {
    let k = s.kind as usize;
    t.s(if k < 13 { SHORT_NAME[k].as_bytes() } else { b"" }).s(b" ");
    let bytes = s.kind == CAPK_ARENA || s.kind == CAPK_DSP_AREA || s.kind == CAPK_STAGE;
    let gt: &[u8] = if compact { b">" } else { b" > " };
    if bytes {
        t.num(s.need.div_ceil(1024)).s(b"K").s(gt).num(s.free / 1024).s(b"K");
    } else {
        t.num(s.need).s(gt).num(s.free);
    }
    if !compact {
        t.s(if s.kind == CAPK_STAGE { b" MAX" } else { b" FREE" });
    }
}

/// The saved selection, matched against the rows: a ticked row for each path the store still has.
unsafe fn load_selection() {
    SEL = core::mem::zeroed();
    dkrt_selection_get(addr_of_mut!(SEL));
    TICK = [0; MAX_ENT];
    SAVED_EXTRA = 0;
    let n = if SEL.count as usize > SEL_MAX { SEL_MAX } else { SEL.count as usize };
    for k in 0..n {
        let p = &SEL.paths[k];
        let mut l = 0;
        while l < SEL_PATH - 1 && p[l] != 0 {
            l += 1;
        }
        let mut found = false;
        for i in 0..NENT {
            if entry_path(ENT[i] as usize) == &p[..l] {
                TICK[i] = 1;
                found = true;
            }
        }
        if !found {
            SAVED_EXTRA += 1;
        }
    }
    SAVED = TICK;
}

/// Scan the store: the runtime lists the files under `modules/`, and a commit of the upload (the MIDI task) that ran during the list makes it start
/// again inside the runtime (finding 43, "the menu's reader and a commit"); the file words read after belong to the same table or fail the load check.
unsafe fn scan_store() {
    if HDR.is_null() {
        HDR = mem::new(512);
    }
    scan_once();
}

unsafe fn scan_once() {
    NENT = 0;
    NOK = [0; MAX_ENT];
    TICK = [0; MAX_ENT];
    SAVED = [0; MAX_ENT];
    SAVED_EXTRA = 0;
    RESULT.clear();
    ARMED = 0;
    let n = dkrt_store_list(b"modules/\0".as_ptr(), addr_of_mut!(ENTRIES) as *mut DkrtStoreEntry, MAX_ENT as u32);
    if n < 0 {
        STORE = if n == E_NOT_FOUND { 2 } else if n == E_READ { 3 } else { 4 };
        return;
    }
    for e in 0..n as usize {
        let p = entry_path(e);
        let l = p.len();
        if l > 4
            && p[l - 4] == b'.'
            && lower(p[l - 3]) == b'd'
            && lower(p[l - 2]) == b'k'
            && lower(p[l - 1]) == b'm'
            && NENT < MAX_ENT
        {
            ENT[NENT] = e as u8;
            NENT += 1;
        }
    }
    STORE = 1;
    let mut st: DkrtStatus = core::mem::zeroed();
    dkrt_status(&mut st);
    HAS_NEEDS = [0; MAX_ENT];
    DK_NEEDS_READS = 0;
    for i in 0..NENT {
        let first = ENTRIES[ENT[i] as usize].first;
        if !compatible(first, st.device) {
            NOK[i] = 1;
        } else if let Some(n) = read_needs(first, ENTRIES[ENT[i] as usize].size) {
            NEEDS[i] = n;
            HAS_NEEDS[i] = 1;
        }
    }
    refresh_loaded();
    load_selection();
}

fn lower(c: u8) -> u8 {
    if c.is_ascii_uppercase() {
        c + 32
    } else {
        c
    }
}
fn upper(c: u8) -> u8 {
    if c.is_ascii_lowercase() {
        c - 32
    } else {
        c
    }
}

// ---- result texts ---------------------------------------------------------------------------------------------------

// A short text (upper case, at most 14 characters) for each runtime result code. One table per owner so that the lanes that
// add codes add lines in their own table: the loader's -1..-19, the registry's -20..-29, the DSP loader's -30..-39, boot-time
// loading's -40..-49. A code without text, or outside, shows as "ERROR <n>". Plain tables: a dense `match` becomes a
// PC-relative jump table, which is not ColdFire code.
const TEXT_RUNTIME: [&str; 20] = [
    "OK", "NOT FOUND", "BAD STORE", "READ ERROR", "BAD SHA", "BAD FORMAT", "RUNTIME OLD", "NO IMPORT", "NO MEMORY",
    "INIT FAILED", "ALREADY LOADED", "SLOT TAKEN", "BUSY", "TOO BIG", "TABLE FULL", "", "", "", "", "",
];
const TEXT_REGISTRY: [&str; 10] = [
    "NO REGISTRY", "OTHER DEVICE", "BAD TYPE", "TYPE USED", "NO PARAM ROW", "NO PAGE ROW", "TYPE IN USE", "", "", "",
];
const TEXT_DSP: [&str; 10] = [
    "NO DSP LINK", "BAD DSP CODE", "DSP IMPORT", "NO DSP SPACE", "NO VOICE ROW", "DSP XFER", "OTHER DEVICE", "BAD VOICE TYPE", "", "",
];
const TEXT_BOOT: [&str; 10] = ["BAD PATH", "TOO MANY", "WRITE FAILED", "CARD BUSY", "SAFE MODE", "", "OUT OF RANGE", "READ ONLY", "", ""];

fn status_text(t: &mut Text, code: i32) {
    let k = code.unsigned_abs() as usize;
    let known = if code >= 0 {
        ""
    } else if k < 20 {
        TEXT_RUNTIME[k]
    } else if k < 30 {
        TEXT_REGISTRY[k - 20]
    } else if k < 40 {
        TEXT_DSP[k - 30]
    } else if k < 50 {
        TEXT_BOOT[k - 40]
    } else {
        ""
    };
    if known.is_empty() {
        t.s(b"ERROR ").int(code);
    } else {
        t.s(known.as_bytes());
    }
}

/// The text of a module's result at boot: `OK`, `LOADING` (the DSP part is still going) or the code's text.
fn result_text(t: &mut Text, code: i32) {
    if code == 1 {
        t.s(b"LOADING");
    } else if code == 0 {
        t.s(b"OK");
    } else {
        status_text(t, code);
    }
}

/// The file name of a path, upper case, at most `max` characters: from the end, or with `head` the first ones.
fn name_of(p: &[u8], max: usize, head: bool, t: &mut Text) {
    let mut start = 0;
    for (k, &c) in p.iter().enumerate() {
        if c == b'/' {
            start = k + 1;
        }
    }
    let name = &p[start..];
    let name = if name.len() <= max {
        name
    } else if head {
        &name[..max]
    } else {
        &name[name.len() - max..]
    };
    for &c in name {
        t.s(&[upper(c)]);
    }
}

/// The row's file name without `.dkm`, upper case, its first `max` characters.
unsafe fn upper_name(i: usize, max: usize, t: &mut Text) {
    let p = entry_path(ENT[i] as usize);
    let l = p.len();
    let p = if l > 4 && p[l - 4] == b'.' && lower(p[l - 3]) == b'd' && lower(p[l - 2]) == b'k' && lower(p[l - 1]) == b'm' { &p[..l - 4] } else { p };
    name_of(p, max, true, t);
}

/// Show `t` in the firmware's message popup window (the one "LOAD ERROR" uses after a project load): it takes the key focus
/// until it is dismissed.
unsafe fn popup(t: &Text) {
    DK_POPUP.b = t.b;
    DK_POPUP.n = t.n;
    DK_POPUPS += 1;
    let text: *const u8 = DK_POPUP.b.as_ptr();
    let mut a = SharedPtr { ptr: 0, ctl: 0 };
    dk_popup_make(&mut a, &text);
    let mut b = SharedPtr { ptr: 0, ctl: 0 };
    string::sp_move(&mut b, &mut a);
    let mgr = view::mgr_of(view::brain());
    view::push(mgr, &b, 0);
    string::sp_release(&mut b.ctl);
    string::sp_release(&mut a.ctl);
}

// ---- boot state -----------------------------------------------------------------------------------------------------

unsafe fn read_boot() {
    BOOT = core::mem::zeroed();
    dkrt_boot_info(addr_of_mut!(BOOT));
}

/// The reason of safe mode as a short text.
fn safe_reason(t: &mut Text, reason: u32) {
    if reason == SAFE_COUNTER {
        t.s(b"2 BAD BOOTS");
    } else if reason == SAFE_KEY {
        t.s(b"KEY HELD");
    } else if reason == SAFE_FLAG {
        t.s(b"REQUESTED");
    } else {
        t.s(b"UNKNOWN");
    }
}

/// Whether the pending selection differs from what the next boot would load: another tick set, a saved path the store lost, or
/// safe mode (this boot loaded nothing; a restart loads the selection).
unsafe fn differs() -> bool {
    if BOOT.safe != 0 || SAVED_EXTRA != 0 {
        return true;
    }
    for i in 0..NENT {
        if TICK[i] != SAVED[i] {
            return true;
        }
    }
    false
}

// ---- the views ------------------------------------------------------------------------------------------------------

const A_NOP: u32 = 0;
const A_OPEN_MAIN: u32 = 1;
const A_OPEN_LOAD: u32 = 2;
const A_OPEN_ABOUT: u32 = 3;
const A_APPLY: u32 = 4;
const A_CLEAR: u32 = 5;
const A_SAFE: u32 = 6;
const A_OPEN_HEALTH: u32 = 7;
const A_DEBUG: u32 = 8;
const A_OPEN_MEM: u32 = 9;
const A_REMOTE: u32 = 10;
const A_OPEN_FXR: u32 = 11;
const A_FXR_APPLY: u32 = 12;
const A_TOGGLE: u32 = 0x100;
const A_FXR_TOGGLE: u32 = 0x200;

unsafe fn fill_main() {
    let m = MAIN.obj;
    let sel = begin_rebuild(m);
    if BOOT.safe != 0 {
        let mut t = Text::new();
        t.s(b"SAFE: ");
        safe_reason(&mut t, BOOT.reason);
        row(m, &t.b, A_SAFE, 0);
    }
    row(m, b"MODULES\0", A_OPEN_LOAD, 0);
    row(m, b"FX ROUTES\0", A_OPEN_FXR, 0);
    row(m, b"ABOUT\0", A_OPEN_ABOUT, 0);
    row(m, b"HEALTH\0", A_OPEN_HEALTH, 0);
    row(m, b"MEMORY\0", A_OPEN_MEM, 0);
    let mut d: DkrtDiag = core::mem::zeroed();
    dkrt_diag(&mut d);
    row(m, if d.debug_on != 0 { b"DEBUG: ON\0" } else { b"DEBUG: OFF\0" }, A_DEBUG, 0);
    // the row exists only in an image that can be remote controlled
    match dkrt_remote_get() {
        1 => row(m, b"REMOTE: ON\0", A_REMOTE, 0),
        0 => row(m, b"REMOTE: OFF\0", A_REMOTE, 0),
        _ => {}
    }
    end_rebuild(m, sel);
}

/// The rows of the MODULES page as text (24 bytes each, NUL-terminated) and how many there are. Read by the proof.
#[no_mangle]
pub static mut DK_LOAD: [[u8; 24]; 40] = [[0; 24]; 40];
#[no_mangle]
pub static mut DK_LOAD_N: u32 = 0;

/// One row of the MODULES page; its text is also kept in `DK_LOAD`.
unsafe fn lrow(m: *mut u8, text: &[u8], act: u32) {
    let k = DK_LOAD_N as usize;
    if k < 40 {
        let mut n = 0;
        while n < 23 && n < text.len() && text[n] != 0 {
            DK_LOAD[k][n] = text[n];
            n += 1;
        }
        while n < 24 {
            DK_LOAD[k][n] = 0;
            n += 1;
        }
        DK_LOAD_N += 1;
    }
    row(m, text, act, 0);
}

/// Characters of a MODULES row: `[X]` the tick, a mark, the name (8), a space and the size (4): 17. The page's list shows about 18 characters (measured on
/// the screenshots of the `fits` scenario: 14.6 pixels of 512 per character, the list from x = 215 to 485); the rest of a longer row is cut off.
const ROW_NAME: usize = 8;

unsafe fn fill_load() {
    let m = LOADV.obj;
    let sel = begin_rebuild(m);
    DK_LOAD_N = 0;
    refit();
    if STORE != 1 {
        let t: &[u8] = match STORE {
            2 => b"NO STORE\0",
            3 => b"STORE READ ERROR\0",
            _ => b"BAD STORE\0",
        };
        lrow(m, t, A_NOP);
    } else if NENT == 0 {
        lrow(m, b"NO MODULES\0", A_NOP);
    } else {
        for i in 0..NENT {
            let mut t = Text::new();
            // `[-]` a file this runtime or device cannot load (it cannot be ticked); otherwise `[X]` ticked (in the pending
            // selection), `[ ]` not; the fourth character is `*` when this boot loaded the module, `L` while it loads its DSP part,
            // `x` when it does not fit next to the ticked ones (`refit`); then the name and the memory it takes (`<1K`, `259K`)
            t.s(if NOK[i] != 0 {
                b"[-]"
            } else if TICK[i] != 0 {
                b"[X]"
            } else {
                b"[ ]"
            });
            t.s(match LOADED[i] {
                0 => {
                    if WONT[i] != 0 {
                        b"x"
                    } else {
                        b" "
                    }
                }
                1 => b"*",
                _ => b"L",
            });
            upper_name(i, ROW_NAME, &mut t);
            if HAS_NEEDS[i] != 0 {
                t.pad(4 + ROW_NAME + 1);
                let mut sz = Text::new();
                size_k(&mut sz, NEEDS[i].arena + NEEDS[i].dsp_area);
                t.pad(4 + ROW_NAME + 1 + 4 - sz.n);
                t.s(&sz.b);
            }
            lrow(m, &t.b, if NOK[i] != 0 { A_NOP } else { A_TOGGLE + i as u32 });
        }
        DK_OFFER = differs() as u32;
        // the memory the ticked set takes, directly above APPLY & RESTART (or where it would be)
        if LOAD_LINE.n > 0 {
            lrow(m, &LOAD_LINE.b, A_NOP);
        }
        if DK_OFFER != 0 {
            lrow(m, if ARMED != 0 { b"CONFIRM RESTART\0" } else { b"APPLY & RESTART\0" }, A_APPLY);
        }
        let mut any = false;
        for i in 0..NENT {
            any |= TICK[i] != 0;
        }
        if any {
            lrow(m, b"CLEAR SELECTION\0", A_CLEAR);
        }
        if BOOT.safe != 0 {
            let mut t = Text::new();
            t.s(b"SAFE: ");
            safe_reason(&mut t, BOOT.reason);
            lrow(m, &t.b, A_SAFE);
        }
        if RESULT.n > 0 {
            lrow(m, &RESULT.b, A_NOP);
        }
    }
    end_rebuild(m, sel);
}

/// The rows of the ABOUT page as text (24 bytes each, NUL-terminated) and how many there are. Read by the proof.
#[no_mangle]
pub static mut DK_ABOUT: [[u8; 24]; 24] = [[0; 24]; 24];
#[no_mangle]
pub static mut DK_ABOUT_N: u32 = 0;

/// One ABOUT row; its text is also kept in `DK_ABOUT`.
unsafe fn arow(m: *mut u8, text: &[u8]) {
    let k = DK_ABOUT_N as usize;
    if k < 24 {
        let mut n = 0;
        while n < 23 && n < text.len() && text[n] != 0 {
            DK_ABOUT[k][n] = text[n];
            n += 1;
        }
        while n < 24 {
            DK_ABOUT[k][n] = 0;
            n += 1;
        }
        DK_ABOUT_N += 1;
    }
    row(m, text, A_NOP, 0);
}

unsafe fn fill_about() {
    let m = ABOUTV.obj;
    let sel = begin_rebuild(m);
    DK_ABOUT_N = 0;
    arow(m, b"DIGIKIT 0.1.0\0");
    let mut st: DkrtStatus = core::mem::zeroed();
    dkrt_status(&mut st);
    read_boot();
    let mut t = Text::new();
    t.s(b"RUNTIME ")
        .num(st.runtime >> 16)
        .s(b".")
        .num(st.runtime >> 8 & 255)
        .s(b".")
        .num(st.runtime & 255);
    arow(m, &t.b);
    let mut d: DkrtDiag = core::mem::zeroed();
    dkrt_diag(&mut d);
    // a card the runtime does not write: why, and what that switches off
    if d.card_state == CARD_FOREIGN || d.card_state == CARD_UNREADABLE {
        arow(m, b"CARD READ-ONLY\0");
        let mut t = Text::new();
        if d.card_state == CARD_FOREIGN {
            t.s(b"NOT OURS AT ").num(d.card_first_nonblank);
        } else {
            t.s(b"CARD UNREADABLE");
        }
        arow(m, &t.b);
        arow(m, b"NO BOOT COUNTER\0");
    }
    if d.dsp_stopped != 0 {
        arow(m, b"DSP STOPPED\0");
        if d.dsp_crumb != 0 {
            // the DSP's last breadcrumb: the step, the voice and the machine type (finding 48)
            let mut t = Text::new();
            t.s(b"LAST: ");
            let st = d.dsp_crumb & 0xff;
            if st == 1 {
                t.s(b"ENTER");
            } else if st == 2 {
                t.s(b"FOUND");
            } else if st == 3 {
                t.s(b"STOCK");
            } else if st == 4 {
                t.s(b"CALL");
            } else if st == 5 {
                t.s(b"RET");
            } else if st == 6 {
                t.s(b"COPY");
            } else if st == 7 {
                t.s(b"DONE");
            } else {
                t.s(b"?");
            }
            t.s(b" V").num((d.dsp_crumb >> 8) & 0xff).s(b" T").num(d.dsp_crumb >> 16);
            arow(m, &t.b);
        }
    }
    if d.crash_found != 0 {
        let mut t = Text::new();
        t.s(b"CRASH PC ").hex(d.crash_pc);
        arow(m, &t.b);
    }
    if BOOT.safe != 0 {
        let mut t = Text::new();
        t.s(b"SAFE MODE: ");
        safe_reason(&mut t, BOOT.reason);
        arow(m, &t.b);
    }
    let mut t = Text::new();
    t.s(b"BOOT: ").num(BOOT.loaded).s(b" OF ").num(BOOT.count).s(b" OK");
    arow(m, &t.b);
    // each module of this boot's selection with its result; the names come from the saved selection, which is what the boot
    // loaded unless it was saved again without a restart
    let mut sel_now: DkrtSelection = core::mem::zeroed();
    dkrt_selection_get(&mut sel_now);
    let n = if BOOT.count as usize > SEL_MAX { SEL_MAX } else { BOOT.count as usize };
    for k in 0..n {
        let mut t = Text::new();
        let p = &sel_now.paths[k];
        let mut l = 0;
        while l < SEL_PATH - 1 && p[l] != 0 {
            l += 1;
        }
        name_of(&p[..l], 17, false, &mut t);
        arow(m, &t.b);
        let mut t = Text::new();
        t.s(b"- ");
        result_text(&mut t, BOOT.results[k]);
        arow(m, &t.b);
    }
    let mut t = Text::new();
    t.s(b"FREE ").num(st.arena_free / 1024).s(b" KB");
    arow(m, &t.b);
    arow(m, if d.ram_kept != 0 { b"RAM KEPT: YES\0" } else { b"RAM KEPT: NO\0" });
    end_rebuild(m, sel);
}

// ---- FX ROUTES (FX routing, docs/findings/62-fx-send-route.md and 64-fx-send-page.md) -------------------------------------------------

/// Effects a page lists at most (the chain has `DK_FX_MAX` = 4 rows).
const MAX_FXR: usize = 4;
const ROUTE_MASTER: u32 = 1;
const ROUTE_SEND: u32 = 2;
/// The loaded effects with a page, as `dkrt_fx` gave them when the page was opened.
static mut FXR: [DkrtFx; MAX_FXR] = unsafe { core::mem::zeroed() };
static mut FXR_N: usize = 0;
/// Per effect: the pending route (`ROUTE_*`; YES changes it, nothing is saved until APPLY & RESTART) and the route the settings give it (the saved one).
static mut FXR_PEND: [u8; MAX_FXR] = [0; MAX_FXR];
static mut FXR_SAVED: [u8; MAX_FXR] = [0; MAX_FXR];
/// The device's send slots (CAPACITY `SEND_SLOTS`, the limit; 0 when the image has no send page).
static mut FXR_SLOTS: u32 = 0;
/// The rows of the FX ROUTES page as text (24 bytes each, NUL-terminated) and how many there are. Read by the proof.
#[no_mangle]
pub static mut DK_FXR: [[u8; 24]; 8] = [[0; 24]; 8];
#[no_mangle]
pub static mut DK_FXR_N: u32 = 0;
/// The pending routes of the effects (1 master, 2 send), for the proof.
#[no_mangle]
pub static mut DK_FXR_PEND: [u8; MAX_FXR] = [0; MAX_FXR];
/// Times APPLY & RESTART wrote the routes (the second press), and the last result of the put, the flush and the restart (which returns only when it refuses).
#[no_mangle]
pub static mut DK_FXR_APPLIES: u32 = 0;
#[no_mangle]
pub static mut DK_FXR_SET: i32 = 0;
#[no_mangle]
pub static mut DK_FXR_FLUSH: i32 = 0;
#[no_mangle]
pub static mut DK_FXR_RESTART: i32 = 0;

/// The effects, the saved routes and the send slots, read from the runtime; the pending routes start as the saved ones.
unsafe fn read_fxr() {
    FXR_N = 0;
    while FXR_N < MAX_FXR {
        if dkrt_fx(FXR_N as u32, addr_of_mut!(FXR[FXR_N])) != OK {
            break;
        }
        FXR_SAVED[FXR_N] = FXR[FXR_N].saved as u8;
        FXR_PEND[FXR_N] = FXR[FXR_N].saved as u8;
        FXR_N += 1;
    }
    let mut caps = [DkrtCap { kind: 0, limit: 0, used: 0, largest: 0 }; CAP_MAX];
    let n = dkrt_capacity(caps.as_mut_ptr(), CAP_MAX as u32) as usize;
    FXR_SLOTS = 0;
    for e in &caps[..n] {
        if e.kind == CAPK_SEND_SLOTS as u32 {
            FXR_SLOTS = e.limit;
        }
    }
    RESULT.clear();
    ARMED = 0;
}

unsafe fn fxr_differs() -> bool {
    for i in 0..FXR_N {
        if FXR_PEND[i] != FXR_SAVED[i] {
            return true;
        }
    }
    false
}

/// One row of the FX ROUTES page; its text is also kept in `DK_FXR`.
unsafe fn frow(m: *mut u8, text: &[u8], act: u32) {
    let k = DK_FXR_N as usize;
    if k < 8 {
        let mut n = 0;
        while n < 23 && n < text.len() && text[n] != 0 {
            DK_FXR[k][n] = text[n];
            n += 1;
        }
        while n < 24 {
            DK_FXR[k][n] = 0;
            n += 1;
        }
        DK_FXR_N += 1;
    }
    row(m, text, act, 0);
}

/// `<TAB>` as the effect's page labels it (up to 3 characters).
fn fxr_tab(t: &mut Text, f: &DkrtFx) {
    let mut n = 0;
    while n < 8 && f.tab[n] != 0 {
        t.s(&f.tab[n..n + 1]);
        n += 1;
    }
}

unsafe fn fill_fxr() {
    let m = FXRV.obj;
    let sel = begin_rebuild(m);
    DK_FXR_N = 0;
    // a row is the effect's tab and the route it will have: `GAN  SEND`, `GAN  MASTER`
    if FXR_N == 0 {
        frow(m, b"NO EFFECTS LOADED\0", A_NOP);
    } else {
        for i in 0..FXR_N {
            let mut t = Text::new();
            fxr_tab(&mut t, &FXR[i]);
            t.pad(5);
            t.s(if FXR_PEND[i] as u32 == ROUTE_SEND { b"SEND" } else { b"MASTER" });
            frow(m, &t.b, A_FXR_TOGGLE + i as u32);
        }
        if fxr_differs() {
            frow(m, if ARMED != 0 { b"CONFIRM RESTART\0" } else { b"APPLY & RESTART\0" }, A_FXR_APPLY);
        }
        if RESULT.n > 0 {
            frow(m, &RESULT.b, A_NOP);
        }
    }
    DK_FXR_PEND = FXR_PEND;
    end_rebuild(m, sel);
}

/// YES on a row: the pending route changes to the other allowed one. An effect that allows one route says which (`GAN: MASTER ONLY`), and a switch to the send
/// route is refused when the pending routes already use every send slot (`NO SEND SLOT`); both are shown in the message row and change nothing.
unsafe fn fxr_toggle(i: usize) {
    RESULT.clear();
    if i < FXR_N {
        let f = &FXR[i];
        let cur = FXR_PEND[i] as u32;
        let other = if cur == ROUTE_SEND { ROUTE_MASTER } else { ROUTE_SEND };
        if f.allowed & other == 0 {
            fxr_tab(&mut RESULT, f);
            RESULT.s(if cur == ROUTE_SEND { b": SEND ONLY" } else { b": MASTER ONLY" });
        } else if other == ROUTE_SEND && fxr_sends(i) >= FXR_SLOTS {
            RESULT.s(b"NO SEND SLOT");
        } else {
            FXR_PEND[i] = other as u8;
        }
    }
    fill_fxr();
}

/// Effects other than `skip` whose pending route is the send bus.
unsafe fn fxr_sends(skip: usize) -> u32 {
    let mut n = 0;
    for i in 0..FXR_N {
        if i != skip && FXR_PEND[i] as u32 == ROUTE_SEND {
            n += 1;
        }
    }
    n
}

/// APPLY & RESTART on the FX ROUTES page: the first press says that the device restarts (a popup) and arms the row; the second writes the changed routes
/// (`fx/route/<module id>`, one byte: 1 send, 0 master), flushes the settings to the card (the write would wait 2 s otherwise) and soft-resets. A refusal is shown.
unsafe fn do_fxr_apply() {
    if ARMED == 0 {
        ARMED = 1;
        RESULT.clear();
        let mut t = Text::new();
        t.s(b"DEVICE WILL RESTART");
        popup(&t);
        return;
    }
    ARMED = 0;
    DK_FXR_APPLIES += 1;
    for i in 0..FXR_N {
        if FXR_PEND[i] != FXR_SAVED[i] {
            let mut key = Text::new();
            key.s(b"fx/route/").s(&FXR[i].id);
            let v = [(FXR_PEND[i] as u32 == ROUTE_SEND) as u8];
            let r = dkrt_settings_put(key.ptr(), v.as_ptr(), 1);
            DK_FXR_SET = r;
            if r != OK {
                fail_text(b"NOT SAVED", r);
                return;
            }
        }
    }
    let r = dkrt_settings_flush();
    DK_FXR_FLUSH = r;
    if r != OK {
        fail_text(b"NOT SAVED", r);
        return;
    }
    FXR_SAVED = FXR_PEND;
    let r = dkrt_restart();
    DK_FXR_RESTART = r;
    fail_text(b"NO RESTART", r);
}

// ---- MEMORY (finding 57) ---------------------------------------------------------------------------------------------------

/// The rows of the MEMORY page as text (24 bytes each, NUL-terminated) and how many there are. Read by the proof.
#[no_mangle]
pub static mut DK_MEM: [[u8; 24]; 8] = [[0; 24]; 8];
#[no_mangle]
pub static mut DK_MEM_N: u32 = 0;

/// The resources the page shows, in order: the CAPACITY kind, the row label and 1 when the row shows a percent (bytes), 0 for `used/limit`.
/// Left out: the staging buffer, the selection, the effect slots and the map entries (they say nothing a user can act on).
const MEM_KINDS: [(u8, &[u8], u8); 7] = [
    (CAPK_DSP_AREA, b"DSP   ", 1),
    (CAPK_ARENA, b"CPU   ", 1),
    (CAPK_MODULES, b"MODS  ", 0),
    (CAPK_MACHINE_ROWS, b"MACH  ", 0),
    (CAPK_VOICE_ROWS, b"VOICE ", 0),
    (CAPK_PARAM_ROWS, b"PARAM ", 0),
    (CAPK_PAGE_ROWS, b"PAGE  ", 0),
];

/// One MEMORY row: the label, a ten-character bar (`|` used, `.` free: both are 2 pixels wide, so the bar keeps its width; `#` is 6 pixels and a full bar runs off the row), then a percent or `used/limit`. Its text is also kept in `DK_MEM`.
unsafe fn mem_row(m: *mut u8, label: &[u8], e: &DkrtCap, pct: bool) {
    let used = if e.used > e.limit { e.limit } else { e.used };
    let full = (used * 10 / e.limit) as usize;
    let mut t = Text::new();
    t.s(label);
    for k in 0..10 {
        t.s(if k < full { b"|" } else { b"." });
    }
    t.s(b" ");
    if pct {
        t.num(used * 100 / e.limit).s(b"%");
    } else {
        t.num(e.used).s(b"/").num(e.limit);
    }
    let k = DK_MEM_N as usize;
    if k < 8 {
        let mut n = 0;
        while n < 23 && t.b[n] != 0 {
            DK_MEM[k][n] = t.b[n];
            n += 1;
        }
        while n < 24 {
            DK_MEM[k][n] = 0;
            n += 1;
        }
        DK_MEM_N += 1;
    }
    row(m, &t.b, A_NOP, 0);
}

unsafe fn fill_mem() {
    let m = MEMV.obj;
    let sel = begin_rebuild(m);
    DK_MEM_N = 0;
    let mut caps = [DkrtCap { kind: 0, limit: 0, used: 0, largest: 0 }; CAP_MAX];
    let n = dkrt_capacity(caps.as_mut_ptr(), CAP_MAX as u32) as usize;
    for &(kind, label, pct) in MEM_KINDS.iter() {
        for e in &caps[..n] {
            // a resource the device does not have (limit 0) has no row
            if e.kind == kind as u32 && e.limit != 0 {
                mem_row(m, label, e, pct != 0);
            }
        }
    }
    end_rebuild(m, sel);
}

// ---- HEALTH (finding 41) --------------------------------------------------------------------------------------------------

/// The figures the page shows (the last `dkrt_health`) and how many times the page was refreshed. Read by the proof.
#[no_mangle]
pub static mut DK_HEALTH: DkrtHealth = unsafe { core::mem::zeroed() };
#[no_mangle]
pub static mut DK_REDRAWS: u32 = 0;
/// Calls of `dk_poll` (UI-loop passes while the page is registered). Read by the proof.
#[no_mangle]
pub static mut DK_POLLS: u32 = 0;
/// 1 once the page was seen open (the polling then stops when it is not), the block count of the last refresh, calls without a clock.
static mut H_SEEN: u32 = 0;
static mut H_LAST: u32 = 0;
static mut H_CALLS: u32 = 0;
/// The page refreshes when this many DSP blocks passed (half a second), or every 60 UI passes without the block clock.
const H_BLOCKS: u32 = 750;

/// `n` bytes as `12.3 MB` (tenths) or, below a megabyte, `511 KB`.
fn size_text(t: &mut Text, n: u32) {
    if n >= 1024 * 1024 {
        let tenth = (n / 1024 * 10) / 1024;
        t.num(tenth / 10).s(b".").num(tenth % 10).s(b" MB");
    } else {
        t.num(n / 1024).s(b" KB");
    }
}

/// A size in KiB as `812 KB`, `12.3 MB` or `21.9 GB` (the store figures: a store can be 22 GiB).
fn size_text_kb(t: &mut Text, kb: u32) {
    if kb >= 1024 * 1024 {
        let tenth = (kb / 1024 * 10) / 1024;
        t.num(tenth / 10).s(b".").num(tenth % 10).s(b" GB");
    } else if kb >= 1024 {
        let tenth = (kb * 10) / 1024;
        t.num(tenth / 10).s(b".").num(tenth % 10).s(b" MB");
    } else {
        t.num(kb).s(b" KB");
    }
}

/// A load in tenths of a percent as `37%`.
fn pct_text(t: &mut Text, x10: u32) {
    t.num((x10 + 5) / 10).s(b"%");
}

/// The page's lines as text, as the last `fill_health` made them (24 bytes each, NUL-terminated). Read by the proof.
#[no_mangle]
pub static mut DK_LINES: [[u8; 24]; 9] = [[0; 24]; 9];

/// One row of the page; its text is also kept in `DK_LINES[k]`.
unsafe fn hrow(m: *mut u8, t: &Text, k: usize) {
    let mut n = 0;
    while n < 23 && t.b[n] != 0 {
        DK_LINES[k][n] = t.b[n];
        n += 1;
    }
    while n < 24 {
        DK_LINES[k][n] = 0;
        n += 1;
    }
    row(m, &t.b, A_NOP, 0);
}

unsafe fn fill_health() {
    let m = HEALTHV.obj;
    let sel = begin_rebuild(m);
    let h = &DK_HEALTH;
    let mut t = Text::new();
    t.s(b"CPU ");
    if h.flags & HEALTH_CPU != 0 {
        pct_text(&mut t, h.cpu_x10);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 0);
    let mut t = Text::new();
    t.s(b"DSP ");
    if h.flags & HEALTH_DSP != 0 {
        pct_text(&mut t, h.dsp_avg_x10);
        t.s(b" MAX ");
        pct_text(&mut t, h.dsp_max_x10);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 1);
    let mut t = Text::new();
    t.s(b"RAM FREE ");
    if h.flags & HEALTH_HEAP != 0 {
        if h.flags & HEALTH_HEAP_CUT != 0 {
            t.s(b">");
        }
        size_text(&mut t, h.heap_free);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 2);
    let mut t = Text::new();
    t.s(b"LARGEST ");
    if h.flags & HEALTH_HEAP != 0 {
        size_text(&mut t, h.heap_largest);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 3);
    let mut t = Text::new();
    t.s(b"CPU FREE ");
    size_text(&mut t, h.arena_free);
    hrow(m, &t, 4);
    let mut t = Text::new();
    t.s(b"DSP FREE ");
    if h.dsp_area_total != 0 {
        size_text(&mut t, h.dsp_area_free);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 5);
    let mut t = Text::new();
    t.s(b"STORE USED ");
    if h.flags & HEALTH_STORE != 0 {
        size_text_kb(&mut t, h.store_used);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 6);
    let mut t = Text::new();
    t.s(b"STORE FREE ");
    if h.flags & HEALTH_STORE != 0 {
        size_text_kb(&mut t, h.store_free);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 7);
    if h.flags & HEALTH_MODULE == 0 {
        let mut t = Text::new();
        t.s(b"NO HEALTH MODULE");
        hrow(m, &t, 8);
    }
    end_rebuild(m, sel);
}

unsafe fn refresh_health() {
    dkrt_health(addr_of_mut!(DK_HEALTH));
    fill_health();
    DK_REDRAWS += 1;
}

/// The UI loop's per-pass call while the HEALTH page is registered: refresh about twice a second without a key press, and stop
/// when the page was closed (the View's parent pointer at +0x2c, which `View::invalidate` tests, is 0 then).
#[no_mangle]
pub unsafe extern "C" fn dk_poll() {
    DK_POLLS = DK_POLLS.wrapping_add(1);
    let v = HEALTHV.obj;
    if v.is_null() {
        dkrt_poll_set(0);
        return;
    }
    let open = *(v.add(VIEW_MGR_AT) as *const u32) != 0;
    if open {
        H_SEEN = 1;
    } else if H_SEEN != 0 {
        dkrt_poll_set(0);
        H_SEEN = 0;
        return;
    } else {
        return;
    }
    let b = dkrt_blocks();
    if b != 0 {
        if H_LAST != 0 && b.wrapping_sub(H_LAST) < H_BLOCKS {
            return;
        }
        H_LAST = b;
    } else {
        H_CALLS += 1;
        if H_CALLS < 60 {
            return;
        }
        H_CALLS = 0;
    }
    refresh_health();
}

/// APPLY & RESTART: the first press says what will happen (a popup) and arms the row; the second saves the pending selection
/// and soft-resets. `dkrt_restart` only returns when it refuses; the refusal is shown.
unsafe fn do_apply() {
    if ARMED == 0 {
        ARMED = 1;
        RESULT.clear();
        let mut t = Text::new();
        t.s(b"UNSAVED CHANGES LOST");
        popup(&t);
        return;
    }
    ARMED = 0;
    DK_APPLIES += 1;
    let mut sel: DkrtSelection = core::mem::zeroed();
    let mut n = 0;
    for i in 0..NENT {
        if TICK[i] != 0 && n < SEL_MAX {
            let p = entry_path(ENT[i] as usize);
            if p.len() < SEL_PATH {
                sel.paths[n][..p.len()].copy_from_slice(p);
                n += 1;
            }
        }
    }
    sel.count = n as u32;
    let r = dkrt_selection_set(&sel);
    DK_APPLY_SET = r;
    if r != OK {
        fail_text(b"NOT SAVED", r);
        return;
    }
    SAVED = TICK;
    SAVED_EXTRA = 0;
    let r = dkrt_restart();
    DK_APPLY_RESTART = r;
    fail_text(b"NO RESTART", r);
}

/// A refusal: the message row and a popup with `what` and the code's text.
unsafe fn fail_text(what: &[u8], code: i32) {
    RESULT.clear();
    status_text(&mut RESULT, code);
    let mut t = Text::new();
    t.s(what).s(b": ");
    status_text(&mut t, code);
    popup(&t);
}

// An `if` chain, not a `match`: dense arms become a PC-relative jump table, which is not ColdFire code.
unsafe fn dispatch(act: u32, ctx: u32) {
    if act == A_OPEN_MAIN {
        let settings = ctx as *mut u8;
        read_boot();
        ensure_view(addr_of_mut!(MAIN), NAME_MAIN, b"DIGIKIT\0", settings);
        fill_main();
        push(settings, addr_of_mut!(MAIN));
        if BOOT.safe != 0 && SAFE_SHOWN == 0 {
            SAFE_SHOWN = 1;
            safe_popup();
        }
        return;
    }
    if act != A_APPLY && act != A_FXR_APPLY {
        ARMED = 0;
    }
    if act == A_OPEN_LOAD {
        read_boot();
        scan_store();
        ensure_view(addr_of_mut!(LOADV), NAME_LOAD, b"MODULES\0", MAIN.obj);
        fill_load();
        push(MAIN.obj, addr_of_mut!(LOADV));
    } else if act == A_OPEN_FXR {
        read_fxr();
        ensure_view(addr_of_mut!(FXRV), NAME_FXR, b"FX ROUTES\0", MAIN.obj);
        fill_fxr();
        push(MAIN.obj, addr_of_mut!(FXRV));
    } else if act == A_FXR_APPLY {
        do_fxr_apply();
        fill_fxr();
    } else if act >= A_FXR_TOGGLE {
        fxr_toggle((act - A_FXR_TOGGLE) as usize);
    } else if act == A_OPEN_ABOUT {
        ensure_view(addr_of_mut!(ABOUTV), NAME_ABOUT, b"ABOUT\0", MAIN.obj);
        text_page(ABOUTV.obj);
        fill_about();
        push(MAIN.obj, addr_of_mut!(ABOUTV));
    } else if act == A_OPEN_HEALTH {
        ensure_view(addr_of_mut!(HEALTHV), NAME_HEALTH, b"HEALTH\0", MAIN.obj);
        text_page(HEALTHV.obj);
        refresh_health();
        push(MAIN.obj, addr_of_mut!(HEALTHV));
        H_SEEN = 0;
        H_LAST = 0;
        H_CALLS = 0;
        dkrt_poll_set(dk_poll as usize);
    } else if act == A_OPEN_MEM {
        ensure_view(addr_of_mut!(MEMV), NAME_MEM, b"MEMORY\0", MAIN.obj);
        text_page(MEMV.obj);
        fill_mem();
        push(MAIN.obj, addr_of_mut!(MEMV));
    } else if act == A_APPLY {
        do_apply();
        fill_load();
    } else if act == A_CLEAR {
        TICK = [0; MAX_ENT];
        RESULT.clear();
        fill_load();
    } else if act == A_DEBUG {
        let mut d: DkrtDiag = core::mem::zeroed();
        dkrt_diag(&mut d);
        let r = dkrt_debug_set((d.debug_on == 0) as u32);
        if r != OK {
            fail_text(b"NOT SAVED", r);
        }
        fill_main();
    } else if act == A_REMOTE {
        dkrt_remote_set((dkrt_remote_get() != 1) as u32);
        fill_main();
    } else if act == A_SAFE {
        safe_popup();
    } else if act >= A_TOGGLE {
        let i = (act - A_TOGGLE) as usize;
        if i < NENT && TICK[i] == 0 && WONT[i] != 0 {
            // a module that does not fit next to the ticked ones is not ticked: say which resource is short
            RESULT.clear();
            short_text(&mut RESULT, &SHORT[i], true);
            let mut t = Text::new();
            t.s(b"NO FIT: ");
            short_text(&mut t, &SHORT[i], false);
            popup(&t);
        } else if i < NENT {
            TICK[i] ^= 1;
            RESULT.clear();
        }
        fill_load();
    }
}

/// Safe mode, in words: why, that no module was loaded, and the way out.
unsafe fn safe_popup() {
    let mut t = Text::new();
    t.s(b"SAFE MODE: ");
    safe_reason(&mut t, BOOT.reason);
    popup(&t);
}

/// Called from the hook at the end of the SETTINGS rebuild (shim.s) with the settings menu: adds DIGIKIT after SYSTEM.
#[no_mangle]
pub unsafe extern "C" fn dk_add_item(menu: *mut u8) {
    add_item(menu, make_item(b"DIGIKIT\0".as_ptr(), A_OPEN_MAIN, menu as u32, true));
}

/// For the test that calls the module in place: the number of `.dkm` entries found by the last scan.
#[no_mangle]
pub unsafe extern "C" fn dk_entry_count() -> u32 {
    NENT as u32
}
