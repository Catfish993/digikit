//! CPU time consumed by the calling thread (exact, from the kernel's
//! per-thread accounting; not a sampling profiler).

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod imp {
    #[repr(C)]
    struct Timespec {
        sec: i64,
        nsec: i64,
    }
    unsafe extern "C" {
        fn clock_gettime(clock: i32, ts: *mut Timespec) -> i32;
    }
    #[cfg(target_os = "macos")]
    const CLOCK_THREAD_CPUTIME_ID: i32 = 16;
    #[cfg(target_os = "linux")]
    const CLOCK_THREAD_CPUTIME_ID: i32 = 3;

    pub fn thread_cpu_ns() -> u64 {
        let mut ts = Timespec { sec: 0, nsec: 0 };
        // SAFETY: `ts` is a valid, writable timespec for the call.
        if unsafe { clock_gettime(CLOCK_THREAD_CPUTIME_ID, &mut ts) } != 0 {
            return 0;
        }
        (ts.sec as u64)
            .saturating_mul(1_000_000_000)
            .saturating_add(ts.nsec as u64)
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod imp {
    pub fn thread_cpu_ns() -> u64 {
        0
    }
}

/// Nanoseconds of CPU time (user + system) the calling thread has used; 0 where
/// the platform has no per-thread clock.
pub fn thread_cpu_ns() -> u64 {
    imp::thread_cpu_ns()
}

#[cfg(test)]
mod tests {
    #[test]
    fn counts_own_busy_time_and_not_sleep() {
        let start = super::thread_cpu_ns();
        let t = std::time::Instant::now();
        let mut x = 0u64;
        // Spin until the thread itself has used 30 ms of CPU (capped by wall time), so a loaded machine
        // (parallel verify) cannot starve the loop below the 20 ms bound.
        while (start == 0 || super::thread_cpu_ns() - start < 30_000_000)
            && t.elapsed() < std::time::Duration::from_secs(5)
        {
            x = x.wrapping_add(std::hint::black_box(1));
            if start == 0 && t.elapsed() >= std::time::Duration::from_millis(30) {
                break;
            }
        }
        let busy = super::thread_cpu_ns() - start;
        std::thread::sleep(std::time::Duration::from_millis(60));
        let after_sleep = super::thread_cpu_ns() - start;
        assert!(x > 0);
        if start != 0 {
            assert!(busy >= 20_000_000, "{busy}");
            assert!(after_sleep - busy < 20_000_000, "{busy} {after_sleep}");
        }
    }
}
