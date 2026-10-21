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
use dkrt_sdk::proto::{CAPK_ARENA, CAPK_DSP_AREA, CAPK_MACHINE_ROWS, CAPK_MODULES, CAPK_PAGE_ROWS, CAPK_PARAM_ROWS, CAPK_VOICE_ROWS};
use dkrt_sdk::{store, DkrtBootInfo, DkrtCap, CAP_MAX, DkrtDiag, DkrtHealth, CARD_FOREIGN, CARD_UNREADABLE, DkrtModule, HEALTH_CPU, HEALTH_DSP, HEALTH_HEAP, HEALTH_HEAP_CUT, HEALTH_MODULE, HEALTH_STORE, DkrtSelection, DkrtStatus, E_NOT_FOUND, E_READ, OK, SAFE_COUNTER, SAFE_FLAG, SAFE_KEY, SEL_MAX, SEL_PATH};

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
static mut HEALTHV: View = NO_VIEW;
static mut MEMV: View = NO_VIEW;

static NAME_MAIN: &[u8] = b"DigikitMenu\0";
static NAME_LOAD: &[u8] = b"DigikitLoadMenu\0";
static NAME_ABOUT: &[u8] = b"DigikitAboutMenu\0";
static NAME_HEALTH: &[u8] = b"DigikitHealthMenu\0";
static NAME_MEM: &[u8] = b"DigikitMemoryMenu\0";

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

// ---- the module store (docs/design/digikit-store.md; the reader is `dkrt_sdk::store`, shared with the runtime) ----------

const MAX_ENT: usize = 32;
/// Table buffer: 128 entries. The card DMA writes it, so it comes from the firmware heap (module RAM stays free of writes by
/// anything but this code); the buddy allocator hands out blocks aligned to their size.
const TAB_BYTES: usize = 16 * 1024;
static mut TAB: *mut u8 = null_mut();

/// 0 = not read, 1 = ok, 2 = no store, 3 = read error, 4 = bad or too big
static mut STORE: u32 = 0;
/// Table indices of the `.dkm` entries.
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

/// Scan the store; a commit of the upload (the MIDI task) that ran during the scan, in the headers' fingerprint, starts it again (at most
/// three times): the table and the file words read must belong to one header (finding 43, "the menu's reader and a commit").
unsafe fn scan_store() {
    if HDR.is_null() {
        HDR = fw_new(512);
    }
    let mut tries = 0;
    loop {
        let before = store::fingerprint(fw_blk_read, HDR);
        scan_once();
        if tries >= 3 || before.is_none() || store::fingerprint(fw_blk_read, HDR) == before {
            break;
        }
        tries += 1;
    }
}

unsafe fn scan_once() {
    NENT = 0;
    NOK = [0; MAX_ENT];
    TICK = [0; MAX_ENT];
    SAVED = [0; MAX_ENT];
    SAVED_EXTRA = 0;
    RESULT.clear();
    ARMED = 0;
    if TAB.is_null() {
        TAB = fw_new(TAB_BYTES as u32);
    }
    let t = match store::open_stable(fw_blk_read, TAB, TAB_BYTES, HDR) {
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
    let mut st: DkrtStatus = core::mem::zeroed();
    dkrt_status(&mut st);
    for i in 0..NENT {
        let f = store::file(TAB, ENT[i] as usize);
        if !compatible(f.first, st.device) {
            NOK[i] = 1;
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

/// The file name of a path, upper case, at most `max` characters from the end.
fn name_of(p: &[u8], max: usize, t: &mut Text) {
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

unsafe fn upper_name(i: usize, max: usize, t: &mut Text) {
    name_of(entry_path(ENT[i] as usize), max, t);
}

/// Show `t` in the firmware's message popup window (the one "LOAD ERROR" uses after a project load): it takes the key focus
/// until it is dismissed.
unsafe fn popup(t: &Text) {
    DK_POPUP.b = t.b;
    DK_POPUP.n = t.n;
    DK_POPUPS += 1;
    let text: *const u8 = DK_POPUP.b.as_ptr();
    let mut a = SharedPtr { ptr: null_mut(), ctl: null_mut() };
    dk_popup_make(&mut a, &text);
    let mut b = SharedPtr { ptr: null_mut(), ctl: null_mut() };
    fw_sp_move(&mut b, &mut a);
    let mgr = fw_mgr_of(fw_brain());
    fw_view_add(mgr, &b, 0);
    fw_sp_release(&mut b.ctl);
    fw_sp_release(&mut a.ctl);
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
const A_TOGGLE: u32 = 0x100;

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
    row(m, b"ABOUT\0", A_OPEN_ABOUT, 0);
    row(m, b"HEALTH\0", A_OPEN_HEALTH, 0);
    row(m, b"MEMORY\0", A_OPEN_MEM, 0);
    let mut d: DkrtDiag = core::mem::zeroed();
    dkrt_diag(&mut d);
    row(m, if d.debug_on != 0 { b"DEBUG: ON\0" } else { b"DEBUG: OFF\0" }, A_DEBUG, 0);
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
            // `[-]` a file this runtime or device cannot load (it cannot be ticked); otherwise `[X]` ticked (in the pending
            // selection), `[ ]` not; the fourth character is `*` when this boot loaded the module, `L` while it loads its DSP part
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
        DK_OFFER = differs() as u32;
        if DK_OFFER != 0 {
            row(m, if ARMED != 0 { b"CONFIRM RESTART\0" } else { b"APPLY & RESTART\0" }, A_APPLY, 0);
        }
        let mut any = false;
        for i in 0..NENT {
            any |= TICK[i] != 0;
        }
        if any {
            row(m, b"CLEAR SELECTION\0", A_CLEAR, 0);
        }
        if BOOT.safe != 0 {
            let mut t = Text::new();
            t.s(b"SAFE: ");
            safe_reason(&mut t, BOOT.reason);
            row(m, &t.b, A_SAFE, 0);
        }
        if RESULT.n > 0 {
            row(m, &RESULT.b, A_NOP, 0);
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
        name_of(&p[..l], 17, &mut t);
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
    (CAPK_ARENA, b"ARENA ", 1),
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
    t.s(b"ARENA FREE ");
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
        size_text(&mut t, h.store_used);
    } else {
        t.s(b"--");
    }
    hrow(m, &t, 6);
    let mut t = Text::new();
    t.s(b"STORE FREE ");
    if h.flags & HEALTH_STORE != 0 {
        size_text(&mut t, h.store_free);
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
    let open = *(v.add(0x38 + 0x2c) as *const u32) != 0;
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
    if act != A_APPLY {
        ARMED = 0;
    }
    if act == A_OPEN_LOAD {
        read_boot();
        scan_store();
        ensure_view(addr_of_mut!(LOADV), NAME_LOAD, b"MODULES\0", MAIN.obj);
        fill_load();
        push(MAIN.obj, addr_of_mut!(LOADV));
    } else if act == A_OPEN_ABOUT {
        ensure_view(addr_of_mut!(ABOUTV), NAME_ABOUT, b"ABOUT\0", MAIN.obj);
        fill_about();
        push(MAIN.obj, addr_of_mut!(ABOUTV));
    } else if act == A_OPEN_HEALTH {
        ensure_view(addr_of_mut!(HEALTHV), NAME_HEALTH, b"HEALTH\0", MAIN.obj);
        refresh_health();
        push(MAIN.obj, addr_of_mut!(HEALTHV));
        H_SEEN = 0;
        H_LAST = 0;
        H_CALLS = 0;
        dkrt_poll_set(dk_poll as usize);
    } else if act == A_OPEN_MEM {
        ensure_view(addr_of_mut!(MEMV), NAME_MEM, b"MEMORY\0", MAIN.obj);
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
    } else if act == A_SAFE {
        safe_popup();
    } else if act >= A_TOGGLE {
        let i = (act - A_TOGGLE) as usize;
        if i < NENT {
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
    add_item(menu, make_item(b"DIGIKIT\0".as_ptr(), A_OPEN_MAIN, menu as u32));
}

/// For the test that calls the module in place: the number of `.dkm` entries found by the last scan.
#[no_mangle]
pub unsafe extern "C" fn dk_entry_count() -> u32 {
    NENT as u32
}
