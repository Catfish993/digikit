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
use dkrt_sdk::{store, DkrtModule, DkrtStatus, E_NOT_FOUND, E_READ, OK};

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
    /// Assembly shim (shim.s): `make_shared<PopupWindow>(const char *const &)` into `out` (A0 = out); `text` points at the
    /// pointer to the NUL-terminated message.
    fn dk_popup_make(out: *mut SharedPtr, text: *const *const u8);
    /// The popup's pointer plumbing as `FUN_400a4832` ("LOAD ERROR") does it: move `src` into a `shared_ptr<View>` `dst`,
    /// the Brain singleton, its view manager (Brain + 0x40), add the view (exclusive flag 0), release a count holder.
    fn fw_sp_move(dst: *mut SharedPtr, src: *mut SharedPtr);
    fn fw_brain() -> *mut u8;
    fn fw_mgr_of(brain: *mut u8) -> *mut u8;
    fn fw_view_add(mgr: *mut u8, sp: *const SharedPtr, exclusive: u32) -> u32;
    fn fw_sp_release(ctl: *mut *mut u8);
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
/// 1 when the runtime has the entry's file loaded (2: the runtime is still loading it), and the module's name for `dkrt_unload`.
static mut LOADED: [u8; MAX_ENT] = [0; MAX_ENT];
static mut MODNAME: [[u8; 32]; MAX_ENT] = [[0; 32]; MAX_ENT];
static mut NENT: usize = 0;
/// 1 when the file's header says this runtime or device cannot load it (shown greyed out, `[-]`, and cannot be ticked).
static mut NOK: [u8; MAX_ENT] = [0; MAX_ENT];
/// One 512-byte, 16-byte aligned firmware heap block the card DMA fills with a file's header sector.
static mut HDR: *mut u8 = null_mut();
/// The answer rows of the last LOAD or UNLOAD: what happened, and for a failure the file it was about (empty: no rows).
static mut RESULT: Text = Text::new();
static mut RESULT2: Text = Text::new();
/// The text of the last popup (the message the firmware's popup window was given) and how many were shown. Read by the proof.
static mut POPUP: Text = Text::new();
static mut POPUPS: u32 = 0;
/// Counters of the start and the end of a LOAD or UNLOAD request (words 0 and 1). Nothing reads them but the proof: a store to
/// either ends a stack epoch of `DIGI_STACK_WATCH_EPOCH`, so the deepest stack use of a request is measured between the two.
static mut MARK: [u32; 2] = [0; 2];
/// A request the runtime accepted and is finishing over later UI-loop passes: 0 none, 1 load, 2 unload; the row it is about.
static mut PEND: u32 = 0;
static mut PEND_ROW: usize = 0;

// What the runtime reports while a request is in flight (the asynchronous loader, `dkrt-sdk` `LOADING`, `DkrtStatus::state`,
// `DkrtModule::state`). An older runtime never answers `LOADING` and its `state` word stays zero, so this works with both: the
// buffers below are larger than the layout this crate was built with, and the words are read by position.
const LOADING: i32 = 1;
const STATE_WORD: usize = 12;
const MOD_STATE_OFF: usize = 112;
const MOD_LOADING: u32 = 1;

#[repr(C)]
struct StatusBuf {
    s: DkrtStatus,
    ext: [u32; 12],
}
#[repr(C)]
struct ModBuf {
    m: DkrtModule,
    ext: [u32; 8],
}

unsafe fn status() -> StatusBuf {
    let mut b: StatusBuf = core::mem::zeroed();
    dkrt_status(&mut b.s);
    b
}
/// `DkrtStatus::state` (0 idle, 1 loading, 2 unloading).
unsafe fn busy(b: &StatusBuf) -> u32 {
    *(b as *const StatusBuf as *const u32).add(STATE_WORD)
}

unsafe fn entry_path(e: usize) -> &'static [u8] {
    store::path(TAB, e)
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
                MODNAME[i] = m.m.name;
            }
        }
    }
}

const DKM_MAGIC: u32 = 0x444b_4d46; // "DKMF"
const REG_MAGIC: u32 = 0x444b_5247; // "DKRG"
/// A header word at byte `off` of the file starting at store sector `first`, read through the block reader; None on an error.
unsafe fn file_word(first: u32, off: u32) -> Option<u32> {
    if store::read_sectors(fw_blk_read, first + off / 512, 1, HDR, 512) != OK {
        return None;
    }
    Some(store::be32(HDR.add((off % 512) as usize)))
}

/// Whether the file at store sector `first` can be loaded by this runtime on this device, judged from its header: the `.dkm`
/// format's `runtime_min`, and the device of its register records (docs/design/digikit-dkm.md; a module without records runs on
/// either device). A file that is not a `.dkm` or cannot be read passes: the runtime then names the real reason when it is ticked.
unsafe fn compatible(first: u32, runtime: u32, device: u32) -> bool {
    match file_word(first, 0) {
        Some(DKM_MAGIC) => {}
        _ => return true,
    }
    if let Some(min) = file_word(first, 0x30) {
        if min > runtime {
            return false;
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

unsafe fn scan_store() {
    NENT = 0;
    NOK = [0; MAX_ENT];
    TICK = [0; MAX_ENT];
    RESULT.clear();
    RESULT2.clear();
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
    if HDR.is_null() {
        HDR = fw_new(512);
    }
    let st = status();
    for i in 0..NENT {
        let f = store::file(TAB, ENT[i] as usize);
        if !compatible(f.first, st.s.runtime, st.s.device) {
            NOK[i] = 1;
        }
    }
    refresh_loaded();
    // a request from before the view was left may have finished
    poll();
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
// add codes add lines in their own table: the loader's -1..-19, the registry's -20..-29, the DSP loader's -30..-39. A code
// that has no text, or lies outside, shows as "ERROR <n>". Plain tables: a dense `match` becomes a PC-relative jump table,
// which is not ColdFire code.
const TEXT_RUNTIME: [&str; 20] = [
    "OK", "NOT FOUND", "BAD STORE", "READ ERROR", "BAD SHA", "BAD FORMAT", "RUNTIME OLD", "NO IMPORT", "NO MEMORY",
    "INIT FAILED", "ALREADY LOADED", "SLOT TAKEN", "BUSY", "TOO BIG", "TABLE FULL", "", "", "", "", "",
];
const TEXT_REGISTRY: [&str; 10] = [
    "NO REGISTRY", "WRONG DEVICE", "BAD TYPE", "TYPE USED", "PARAMS FULL", "PAGES FULL", "IN USE", "", "", "",
];
const TEXT_DSP: [&str; 10] = [
    "NO DSP LINK", "BAD DSP CODE", "DSP IMPORT", "DSP FULL", "NO VOICE ROW", "DSP TRANSFER", "WRONG DEVICE", "BAD VOICE", "", "",
];

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
    } else {
        ""
    };
    if known.is_empty() {
        t.s(b"ERROR ").int(code);
    } else {
        t.s(known.as_bytes());
    }
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

/// Show `POPUP` in the firmware's message popup window (the one "LOAD ERROR" uses after a project load): it takes the key focus
/// until it is dismissed.
unsafe fn popup(t: &Text) {
    POPUP.b = t.b;
    POPUP.n = t.n;
    POPUPS += 1;
    let text: *const u8 = POPUP.b.as_ptr();
    let mut a = SharedPtr { ptr: null_mut(), ctl: null_mut() };
    dk_popup_make(&mut a, &text);
    let mut b = SharedPtr { ptr: null_mut(), ctl: null_mut() };
    fw_sp_move(&mut b, &mut a);
    let mgr = fw_mgr_of(fw_brain());
    fw_view_add(mgr, &b, 0);
    fw_sp_release(&mut b.ctl);
    fw_sp_release(&mut a.ctl);
}

/// A request for row `i` failed with `code`: the answer rows name the reason and the file, and a popup says both.
unsafe fn fail(i: usize, code: i32) {
    RESULT.clear();
    RESULT2.clear();
    status_text(&mut RESULT, code);
    upper_name(i, 17, &mut RESULT2);
    let mut t = Text::new();
    upper_name(i, 24, &mut t);
    t.s(b": ");
    status_text(&mut t, code);
    popup(&t);
}

/// Collect the end of a request the runtime was finishing: once its state is idle again, `last_result` is the answer.
unsafe fn poll() {
    if PEND == 0 {
        return;
    }
    let st = status();
    if busy(&st) != 0 {
        return;
    }
    let (row, kind) = (PEND_ROW, PEND);
    PEND = 0;
    refresh_loaded();
    RESULT.clear();
    RESULT2.clear();
    if st.s.last_result == OK {
        TICK[row] = 0;
        RESULT.s(if kind == 1 { b"LOADED 1" } else { b"UNLOADED 1" });
    } else {
        fail(row, st.s.last_result);
    }
}

// ---- the three views ---------------------------------------------------------------------------------------------

const A_NOP: u32 = 0;
const A_OPEN_MAIN: u32 = 1;
const A_OPEN_LOAD: u32 = 2;
const A_OPEN_ABOUT: u32 = 3;
const A_LOAD: u32 = 4;
const A_UNLOAD: u32 = 5;
const A_REFRESH: u32 = 6;
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
            // `[-]` a file this runtime or device cannot load (it cannot be ticked); otherwise `[X]` ticked, `[ ]` not; the fourth character is `*` when the runtime has the module loaded, `L` while it loads it
            t.s(if NOK[i] != 0 {
                b"[-]"
            } else if TICK[i] != 0 {
                b"[X]"
            } else {
                b"[ ]"
            });
            t.s(match LOADED[i] {
                0 => b" ",
                1 => b"*",
                _ => b"L",
            });
            upper_name(i, 17, &mut t);
            row(m, &t.b, if NOK[i] != 0 { A_NOP } else { A_TOGGLE + i as u32 }, 0);
        }
        row(m, b"LOAD\0", A_LOAD, 0);
        row(m, b"UNLOAD\0", A_UNLOAD, 0);
        // the answer rows; pressing YES on the first asks the runtime again (a request may still be in flight)
        if RESULT.n > 0 {
            row(m, &RESULT.b, A_REFRESH, 0);
        }
        if RESULT2.n > 0 {
            row(m, &RESULT2.b, A_REFRESH, 0);
        }
    }
    end_rebuild(m, sel);
}

unsafe fn fill_about() {
    let m = ABOUTV.obj;
    let sel = begin_rebuild(m);
    row(m, b"DIGIKIT 0.1.0\0", A_NOP, 0);
    let st = status();
    let mut t = Text::new();
    t.s(b"RUNTIME ABI ").num(st.s.runtime);
    row(m, &t.b, A_NOP, 0);
    let mut t = Text::new();
    t.s(b"LOADED ").num(st.s.modules);
    row(m, &t.b, A_NOP, 0);
    let mut t = Text::new();
    t.s(b"FREE ").num(st.s.arena_free / 1024).s(b" KB");
    row(m, &t.b, A_NOP, 0);
    end_rebuild(m, sel);
}

/// The summary of a batch of requests that all finished at once.
unsafe fn summarise(done: u32, failed: u32, first_fail: Option<(usize, i32)>, past: &[u8]) {
    RESULT.clear();
    RESULT2.clear();
    if let Some((i, code)) = first_fail {
        fail(i, code);
        if failed > 1 {
            RESULT.s(b" +").num(failed - 1);
        }
    } else if done == 0 {
        RESULT.s(b"NOTHING TO DO");
    } else {
        RESULT.s(past).s(b" ").num(done);
    }
}

/// LOAD: load every ticked row the runtime does not have. A request that the runtime accepts and finishes later (`LOADING`)
/// ends the batch; the answer rows show `LOADING...` until `poll` sees the runtime idle again. While the runtime is busy, a
/// second request is refused.
unsafe fn do_load() {
    mark(0);
    do_load_inner();
    mark(1);
}

unsafe fn mark(k: usize) {
    let p = addr_of_mut!(MARK) as *mut u32;
    p.add(k).write_volatile(p.add(k).read_volatile().wrapping_add(1));
}

unsafe fn do_load_inner() {
    poll();
    if busy(&status()) != 0 || PEND != 0 {
        RESULT.clear();
        RESULT2.clear();
        RESULT.s(b"LOADING...");
        return;
    }
    let (mut done, mut failed) = (0u32, 0u32);
    let mut first_fail: Option<(usize, i32)> = None;
    for i in 0..NENT {
        if TICK[i] == 0 || LOADED[i] != 0 {
            continue;
        }
        let p = entry_path(ENT[i] as usize);
        let mut path = [0u8; 88];
        path[..p.len()].copy_from_slice(p);
        let r = dkrt_load(path.as_ptr());
        if r == LOADING {
            PEND = 1;
            PEND_ROW = i;
            break;
        } else if r == OK {
            done += 1;
            TICK[i] = 0;
        } else {
            failed += 1;
            if first_fail.is_none() {
                first_fail = Some((i, r));
            }
        }
    }
    refresh_loaded();
    if PEND != 0 {
        RESULT.clear();
        RESULT2.clear();
        RESULT.s(b"LOADING...");
    } else {
        summarise(done, failed, first_fail, b"LOADED");
    }
}

/// UNLOAD: unload every ticked row the runtime has loaded (ticks alone never unload anything).
unsafe fn do_unload() {
    mark(0);
    do_unload_inner();
    mark(1);
}

unsafe fn do_unload_inner() {
    poll();
    if busy(&status()) != 0 || PEND != 0 {
        RESULT.clear();
        RESULT2.clear();
        RESULT.s(b"LOADING...");
        return;
    }
    let (mut done, mut failed) = (0u32, 0u32);
    let mut first_fail: Option<(usize, i32)> = None;
    for i in 0..NENT {
        if TICK[i] == 0 || LOADED[i] != 1 {
            continue;
        }
        let r = dkrt_unload(MODNAME[i].as_ptr());
        if r == LOADING {
            PEND = 2;
            PEND_ROW = i;
            break;
        } else if r == OK {
            done += 1;
            TICK[i] = 0;
        } else {
            failed += 1;
            if first_fail.is_none() {
                first_fail = Some((i, r));
            }
        }
    }
    refresh_loaded();
    if PEND != 0 {
        RESULT.clear();
        RESULT2.clear();
        RESULT.s(b"LOADING...");
    } else {
        summarise(done, failed, first_fail, b"UNLOADED");
    }
}

// An `if` chain, not a `match`: six dense arms become a PC-relative jump table, which is not ColdFire code.
unsafe fn dispatch(act: u32, ctx: u32) {
    if act == A_OPEN_MAIN {
        let settings = ctx as *mut u8;
        ensure_view(addr_of_mut!(MAIN), NAME_MAIN, b"DIGIKIT\0", settings);
        fill_main();
        push(settings, addr_of_mut!(MAIN));
    } else if act == A_OPEN_LOAD {
        scan_store();
        ensure_view(addr_of_mut!(LOADV), NAME_LOAD, b"MODULES\0", MAIN.obj);
        fill_load();
        push(MAIN.obj, addr_of_mut!(LOADV));
    } else if act == A_OPEN_ABOUT {
        ensure_view(addr_of_mut!(ABOUTV), NAME_ABOUT, b"ABOUT\0", MAIN.obj);
        fill_about();
        push(MAIN.obj, addr_of_mut!(ABOUTV));
    } else if act == A_LOAD {
        do_load();
        fill_load();
    } else if act == A_UNLOAD {
        do_unload();
        fill_load();
    } else if act == A_REFRESH {
        poll();
        refresh_loaded();
        fill_load();
    } else if act >= A_TOGGLE {
        let i = (act - A_TOGGLE) as usize;
        poll();
        if i < NENT {
            TICK[i] ^= 1;
            if PEND == 0 {
                RESULT.clear();
                RESULT2.clear();
            }
        }
        fill_load();
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
