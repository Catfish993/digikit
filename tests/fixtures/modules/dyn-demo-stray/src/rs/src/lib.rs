//! test/dyn-demo-stray: test/dyn-demo plus one store outside the module's own block, for the ColdFire memory audit
//! (`mise run module:audit`, tests/digikit/scenarios/memaudit-*.toml). Slot call number `STRAY_AT` writes one word `STRAY_OFFSET` bytes
//! above the module's counter: an address in the arena that this module does not own. The audit arms its guard after the boot, so the
//! call number lies in the audit's window. The audit must report exactly this one store.
#![no_std]

use core::panic::PanicInfo;
use dkrt_sdk::Api;

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

pub const MAGIC: u32 = 0x4459_4e4d; // "DYNM"
/// Distance of the stray store from the counter: far above the module's block, inside the 512 KiB arena.
pub const STRAY_OFFSET: usize = 0x2_0000;

/// The slot call that makes the stray store (once). The counter is at 15 when the audit arms and about 1200 at its end.
pub const STRAY_AT: u32 = 500;

/// [0] MAGIC after the first slot call, [1] the number of slot calls. Aligned to a cache line: the data shares no line with the code.
#[repr(align(16))]
pub struct Counter(pub [u32; 4]);

#[no_mangle]
pub static mut DYN_COUNTER: Counter = Counter([0; 4]);

#[no_mangle]
pub unsafe extern "C" fn dkm_init(_api: *const Api) -> i32 {
    let c = &raw mut DYN_COUNTER;
    c.write(Counter([0; 4]));
    0
}

#[no_mangle]
pub unsafe extern "C" fn dkm_slot_0(_api: *const Api) {
    let c = &raw mut DYN_COUNTER as *mut u32;
    let n = c.add(1).read_volatile();
    if n == STRAY_AT {
        // the stray store, once
        (c as *mut u8).add(STRAY_OFFSET).cast::<u32>().write_volatile(0xdead_0001);
    }
    c.write_volatile(MAGIC);
    c.add(1).write_volatile(n.wrapping_add(1));
}

#[no_mangle]
pub unsafe extern "C" fn dkm_fini(_api: *const Api) {}
