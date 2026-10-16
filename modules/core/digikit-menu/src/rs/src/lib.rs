//! core/digikit-menu: SETTINGS > DIGIKIT (docs/findings/28-digikit-menu.md).
//!
//! Built with rustc_codegen_gcc for the ColdFire (finding 26). The firmware's own menu classes are used as they
//! are (finding 23, S2): a `MenuItem` built by the firmware's builder, `VerticalMenuView` objects, `std::function`
//! objects with our own manager and invoker, and the view stack push. Every firmware function is an undefined
//! symbol that module.toml maps to an anchor; nothing here knows an address.
//!
//! Boundary rules (finding 23, S1b): integers and pointers only; the firmware calls us with the gcc m68k cdecl
//! convention (arguments on the stack, result in d0), which Rust's `extern "C"` matches.
#![no_std]
#![allow(static_mut_refs, dangerous_implicit_autorefs)]

use core::mem::transmute;
use core::panic::PanicInfo;
use core::ptr::{addr_of, addr_of_mut, null_mut};
use dkrt_sdk::{result_text, store, DkrtModule, DkrtStatus, E_NOT_FOUND, E_READ, OK};

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

// ---- firmware functions (anchors) ----------------------------------------------------------------------------

extern "C" {
    /// `operator new(size)`.
    fn fw_new(size: u32) -> *mut u8;
    /// `(item, label cstr, &action, icon, &fn4)`: constructs a MenuItem (0x54 bytes) in `item`; copies the functions.
    fn fw_item_builder(item: *mut u8, label: *const u8, action: *const StdFn, icon: *const u8, fn4: *const StdFn);
    /// `VerticalMenuView(this, name cstr, reserve, const std::string *title, icon)`; the object is 0x1ac bytes.
    fn fw_view_ctor(this: *mut u8, name: *const u8, reserve: u32, title: *const u32, icon: *const u8);
    /// `View::push`: `(menu, &shared_ptr<View>)`; `menu` is the object that opens the view.
    fn fw_push(menu: *mut u8, sp: *const SharedPtr);
    /// COW `std::string(cstr, allocator)` into `s` (one pointer) and its destructor.
    fn fw_str_ctor(s: *mut u32, cstr: *const u8, alloc: *const u8);
    fn fw_str_dtor(s: *mut u32);
    /// `View::invalidate(View *)`: asks for a redraw of the view (the screen is redrawn only when a view says so).
    fn fw_invalidate(view: *mut u8);
    /// The eMMC block read: `(sector, byte count, buffer)`; 0 = ok. Buffer 16-byte aligned, count a multiple of 512.
    fn fw_blk_read(sector: u32, bytes: u32, buf: *mut u8) -> i32;
    /// The `pea (icon).l` instruction of the SYSTEM item (6 bytes of code); its operand is the address of a stock icon
    /// object built at run time.
    static fw_icon_ref: [u16; 3];
    /// Assembly shim (shim.s): `shared_ptr<View>` from a raw `View*` with the opener as parent (A0 = out).
    fn dk_make_sp(out: *mut SharedPtr, parent: *mut u8, view: *mut u8);
}

// ---- the runtime (core/runtime) --------------------------------------------------------------------------------

// The menu `requires` core/runtime, so these are plain symbols the linker resolves to the runtime's exports. The layouts
// (`DkrtStatus`, `DkrtModule`) and the result codes are the runtime's own, from the shared crate `dkrt-sdk`.
extern "C" {
    /// Load the `.dkm` at the store path (relative to `digikit/`, for example `modules/a.dkm`); 0 = loaded, else an `E_*` code.
    fn dkrt_load(path: *const u8) -> i32;
    /// Unload the module called `name` (the name in the `.dkm` header, from `DkrtModule`).
    fn dkrt_unload(name: *const u8) -> i32;
    fn dkrt_status(out: *mut DkrtStatus);
    /// The `index`th loaded module; 0, or `E_NOT_FOUND` past the end.
    fn dkrt_module(index: u32, out: *mut DkrtModule) -> i32;
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

/// `std::shared_ptr<View>`: pointer and control block.
#[repr(C)]
pub struct SharedPtr {
    ptr: *mut u8,
    ctl: *mut u8,
}

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

static NAME_MAIN: &[u8] = b"DigikitMenu\0";
static NAME_LOAD: &[u8] = b"DigikitLoadMenu\0";
static NAME_ABOUT: &[u8] = b"DigikitAboutMenu\0";

/// The address of the stock icon the SYSTEM item uses, read from the operand of the instruction that pushes it.
unsafe fn icon() -> *const u8 {
    let r = addr_of!(fw_icon_ref) as *const u16;
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
    fw_invalidate(menu.add(0x38));
}

/// One MenuItem with a text label and an action; returned to the caller, which hands it to `add_item`.
unsafe fn make_item(label: *const u8, act: u32, ctx: u32) -> *mut u8 {
    let item = fw_new(0x54);
    let mut action = make_fn(act, ctx);
    let mut fn4 = EMPTY_FN;
    fw_item_builder(item, label, &action, icon(), &fn4);
    // the builder copied both; destroy the temporaries as the firmware does (fn4 first)
    fn_manager(addr_of_mut!(fn4) as *mut u32, addr_of!(fn4) as *const u32, 3);
    fn_manager(addr_of_mut!(action) as *mut u32, addr_of!(action) as *const u32, 3);
    item
}

unsafe fn row(menu: *mut u8, text: &[u8], act: u32, ctx: u32) {
    add_item(menu, make_item(text.as_ptr(), act, ctx));
}

/// Build a stock VerticalMenuView once; `parent` is the object that will push it.
unsafe fn ensure_view(v: *mut View, name: &'static [u8], title: &'static [u8], parent: *mut u8) {
    if !(*v).obj.is_null() {
        return;
    }
    let obj = fw_new(0x1ac);
    let mut s: u32 = 0;
    let alloc: u8 = 0;
    fw_str_ctor(&mut s, title.as_ptr(), &alloc);
    fw_view_ctor(obj, name.as_ptr(), 8, &s, icon());
    fw_str_dtor(&mut s);
    (*v).obj = obj;
    // the View sub-object sits at +0x38
    (*v).sp = fw_new(8) as *mut SharedPtr;
    dk_make_sp((*v).sp, parent, obj.add(0x38));
}

unsafe fn push(from: *mut u8, v: *mut View) {
    fw_push(from, (*v).sp);
}

// ---- small text helpers ------------------------------------------------------------------------------------------

#[repr(C)]
struct Text {
    b: [u8; 40],
    n: usize,
}
impl Text {
    fn new() -> Text {
        Text { b: [0; 40], n: 0 }
    }
    fn s(&mut self, t: &[u8]) -> &mut Text {
        for &c in t {
            if self.n < 39 && c != 0 {
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
    fn ptr(&self) -> *const u8 {
        self.b.as_ptr() // zero filled: always terminated
    }
}

// ---- the module store (docs/design/digikit-store.md; the reader is `dkrt_sdk::store`, shared with the runtime) ----------

const MAX_ENT: usize = 32;
/// Table buffer: 128 entries. The card DMA writes it, so it comes from the firmware heap (module RAM stays free of writes by
/// anything but this code); the buddy allocator hands out blocks aligned to their size.
const TAB_BYTES: usize = 16 * 1024;
static mut TAB: *mut u8 = null_mut();

/// 0 = not read, 1 = ok, 2 = no store, 3 = read error, 4 = bad or too big
static mut STORE: u32 = 0;
/// Table indices of the `.dkm` entries, and their ticks.
static mut ENT: [u8; MAX_ENT] = [0; MAX_ENT];
static mut TICK: [u8; MAX_ENT] = [0; MAX_ENT];
/// 1 when the runtime has the entry's file loaded, and the module's name for `dkrt_unload`.
static mut LOADED: [u8; MAX_ENT] = [0; MAX_ENT];
static mut MODNAME: [[u8; 32]; MAX_ENT] = [[0; 32]; MAX_ENT];
static mut NENT: usize = 0;
/// The result row of the last LOAD (empty: shows "LOAD").
static mut RESULT: Text = Text { b: [0; 40], n: 0 };

unsafe fn entry_path(e: usize) -> &'static [u8] {
    store::path(TAB, e)
}

/// Ask the runtime which modules it holds and match their store paths against the rows.
unsafe fn refresh_loaded() {
    LOADED = [0; MAX_ENT];
    let mut index = 0;
    loop {
        let mut m: DkrtModule = core::mem::zeroed();
        if dkrt_module(index, &mut m) != OK {
            break;
        }
        index += 1;
        let mut n = 0;
        while n < 63 && m.path[n] != 0 {
            n += 1;
        }
        for i in 0..NENT {
            if entry_path(ENT[i] as usize) == &m.path[..n] {
                LOADED[i] = 1;
                MODNAME[i] = m.name;
            }
        }
    }
}

unsafe fn scan_store() {
    NENT = 0;
    TICK = [0; MAX_ENT];
    RESULT.n = 0;
    RESULT.b = [0; 40];
    if TAB.is_null() {
        TAB = fw_new(TAB_BYTES as u32);
    }
    let t = match store::open(fw_blk_read, TAB, TAB_BYTES) {
        Ok(t) => t,
        Err(e) => {
            STORE = if e == E_NOT_FOUND { 2 } else if e == E_READ { 3 } else { 4 };
            return;
        }
    };
    for e in 0..t.count as usize {
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
    refresh_loaded();
    TICK = LOADED; // a loaded module starts ticked: unticking it and pressing LOAD unloads it
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

// ---- the three views ---------------------------------------------------------------------------------------------

const A_NOP: u32 = 0;
const A_OPEN_MAIN: u32 = 1;
const A_OPEN_LOAD: u32 = 2;
const A_OPEN_ABOUT: u32 = 3;
const A_LOAD: u32 = 4;
const A_TOGGLE: u32 = 0x100;

unsafe fn fill_main() {
    let m = MAIN.obj;
    let sel = begin_rebuild(m);
    row(m, b"LOAD MODULES\0", A_OPEN_LOAD, 0);
    row(m, b"ABOUT\0", A_OPEN_ABOUT, 0);
    end_rebuild(m, sel);
}

unsafe fn fill_load() {
    let m = LOADV.obj;
    let sel = begin_rebuild(m);
    if STORE != 1 {
        let t: &[u8] = match STORE {
            2 => b"NO STORE\0",
            3 => b"STORE READ ERROR\0",
            _ => b"BAD STORE\0",
        };
        row(m, t, A_NOP, 0);
    } else if NENT == 0 {
        row(m, b"NO MODULES\0", A_NOP, 0);
    } else {
        for i in 0..NENT {
            let mut t = Text::new();
            // `[X]` ticked, `[ ]` not; the fourth character is `*` when the runtime has the module loaded
            t.s(if TICK[i] != 0 { b"[X]" } else { b"[ ]" });
            t.s(if LOADED[i] != 0 { b"*" } else { b" " });
            upper_name(i, 17, &mut t);
            row(m, &t.b, A_TOGGLE + i as u32, 0);
        }
        if RESULT.n > 0 {
            row(m, &RESULT.b, A_LOAD, 0);
        } else {
            row(m, b"LOAD\0", A_LOAD, 0);
        }
    }
    end_rebuild(m, sel);
}

unsafe fn fill_about() {
    let m = ABOUTV.obj;
    let sel = begin_rebuild(m);
    row(m, b"DIGIKIT 0.1.0\0", A_NOP, 0);
    let mut st: DkrtStatus = core::mem::zeroed();
    dkrt_status(&mut st);
    let mut t = Text::new();
    t.s(b"RUNTIME ABI ").num(st.runtime);
    row(m, &t.b, A_NOP, 0);
    let mut t = Text::new();
    t.s(b"LOADED ").num(st.modules);
    row(m, &t.b, A_NOP, 0);
    let mut t = Text::new();
    t.s(b"FREE ").num(st.arena_free / 1024).s(b" KB");
    row(m, &t.b, A_NOP, 0);
    end_rebuild(m, sel);
}

/// The file name of a row, upper case, at most `max` characters from the end.
unsafe fn upper_name(i: usize, max: usize, t: &mut Text) {
    let p = entry_path(ENT[i] as usize);
    let mut start = 0;
    for (k, &c) in p.iter().enumerate() {
        if c == b'/' {
            start = k + 1;
        }
    }
    let name = &p[start..];
    let name = if name.len() > max { &name[name.len() - max..] } else { name };
    for &c in name {
        t.s(&[upper(c)]);
    }
}

/// LOAD: bring the runtime in line with the ticks. A ticked row that is not loaded is loaded, an unticked row that is loaded
/// is unloaded. The answer row shows the counts, or the first failure with the runtime's result text ("BAD SHA", "NO IMPORT").
unsafe fn do_load() {
    RESULT.b = [0; 40];
    RESULT.n = 0;
    let (mut loaded, mut unloaded, mut failed) = (0u32, 0u32, 0u32);
    let mut first_fail: Option<(usize, i32)> = None;
    for i in 0..NENT {
        let want = TICK[i] != 0;
        if want == (LOADED[i] != 0) {
            continue;
        }
        let r = if want {
            let p = entry_path(ENT[i] as usize);
            let mut path = [0u8; 88];
            path[..p.len()].copy_from_slice(p);
            dkrt_load(path.as_ptr())
        } else {
            dkrt_unload(MODNAME[i].as_ptr())
        };
        if r == OK {
            if want {
                loaded += 1;
            } else {
                unloaded += 1;
            }
        } else {
            failed += 1;
            if first_fail.is_none() {
                first_fail = Some((i, r));
            }
        }
    }
    refresh_loaded();
    if let Some((_i, r)) = first_fail {
        // the row is about 17 characters wide; the failed rows stay ticked and unmarked, so they show which one it was
        RESULT.s(result_text(r).as_bytes());
        if failed > 1 {
            RESULT.s(b" +").num(failed - 1);
        }
    } else if loaded + unloaded == 0 {
        RESULT.s(b"NOTHING TO DO");
    } else if unloaded == 0 {
        RESULT.s(b"LOADED ").num(loaded);
    } else if loaded == 0 {
        RESULT.s(b"UNLOADED ").num(unloaded);
    } else {
        RESULT.s(b"LOADED ").num(loaded).s(b" UNLOAD ").num(unloaded);
    }
}

unsafe fn dispatch(act: u32, ctx: u32) {
    match act {
        A_OPEN_MAIN => {
            let settings = ctx as *mut u8;
            ensure_view(addr_of_mut!(MAIN), NAME_MAIN, b"DIGIKIT\0", settings);
            fill_main();
            push(settings, addr_of_mut!(MAIN));
        }
        A_OPEN_LOAD => {
            scan_store();
            ensure_view(addr_of_mut!(LOADV), NAME_LOAD, b"MODULES\0", MAIN.obj);
            fill_load();
            push(MAIN.obj, addr_of_mut!(LOADV));
        }
        A_OPEN_ABOUT => {
            ensure_view(addr_of_mut!(ABOUTV), NAME_ABOUT, b"ABOUT\0", MAIN.obj);
            fill_about();
            push(MAIN.obj, addr_of_mut!(ABOUTV));
        }
        A_LOAD => {
            do_load();
            fill_load();
        }
        a if a >= A_TOGGLE => {
            let i = (a - A_TOGGLE) as usize;
            if i < NENT {
                TICK[i] ^= 1;
                RESULT.n = 0;
                RESULT.b = [0; 40];
            }
            fill_load();
        }
        _ => {}
    }
}

/// Called from the hook at the end of the SETTINGS rebuild (shim.s) with the settings menu: adds DIGIKIT after SYSTEM.
#[no_mangle]
pub unsafe extern "C" fn dk_add_item(menu: *mut u8) {
    add_item(menu, make_item(b"DIGIKIT\0".as_ptr(), A_OPEN_MAIN, menu as u32));
}

/// For the test that calls the module in place: the number of `.dkm` entries found by the last scan.
#[no_mangle]
pub unsafe extern "C" fn dk_entry_count() -> u32 {
    NENT as u32
}
