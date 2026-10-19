//! test/dyn-demo: the smallest module the digikit runtime can load. Packed into `dyn-demo.dkm` by `patchkit pack`.
//!
//! `dkm_slot_0` runs once per pass of the UI task's event loop while the module is loaded: it stores a magic and adds one
//! to a counter in the module's own bss (in the arena). `dkm_init` zeroes the counter and logs its address (the log ring
//! is in the runtime's state); `dkm_fini` leaves a marker behind.
#![no_std]

use core::panic::PanicInfo;
use dkrt_sdk::Api;

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

pub const MAGIC: u32 = 0x4459_4e4d; // "DYNM"
pub const FINI_MARK: u32 = 0xf1f1_0001;

/// [0] MAGIC after the first slot call, [1] the number of slot calls, [2] FINI_MARK after `dkm_fini`, [3] unused.
#[no_mangle]
pub static mut DYN_COUNTER: [u32; 4] = [0; 4];

#[no_mangle]
pub unsafe extern "C" fn dkm_init(api: *const Api) -> i32 {
    let c = &raw mut DYN_COUNTER;
    c.write([0; 4]);
    ((*api).log)(b"dyn-demo counter\0".as_ptr(), c as u32);
    0
}

#[no_mangle]
pub unsafe extern "C" fn dkm_slot_0(_api: *const Api) {
    let c = &raw mut DYN_COUNTER as *mut u32;
    c.write_volatile(MAGIC);
    c.add(1)
        .write_volatile(c.add(1).read_volatile().wrapping_add(1));
}

#[no_mangle]
pub unsafe extern "C" fn dkm_fini(api: *const Api) {
    let c = &raw mut DYN_COUNTER as *mut u32;
    c.add(2).write_volatile(FINI_MARK);
    ((*api).log)(b"dyn-demo fini\0".as_ptr(), c.add(1).read_volatile());
}
