//! CPU time of the ColdFire side alone, restored from a coupled ready snapshot
//! with no DSP attached: `cf_snap_bench SYX SNAPSHOT [SECONDS]` (emulated
//! seconds, 132M instructions each). The same workload as `digi_load_snapshot`
//! in the wasm core, so native and wasm numbers compare.
use elektron_native_boot::{Emulator, thread_cpu_ns};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let syx = std::fs::read(&args[1]).expect("syx");
    let snap = std::fs::read(&args[2]).expect("snapshot");
    let seconds: f64 = args.get(3).map_or(5.0, |s| s.parse().expect("seconds"));
    let mut emulator = Emulator::new(&syx, None).expect("emulator");
    emulator.enable_ssi_diagnostic(96_000).expect("ssi");
    emulator.load_state(&snap).expect("load_state");
    let start = emulator.icount();
    let target = start + (seconds * 132e6) as u64;
    let t0 = thread_cpu_ns();
    while emulator.icount() < target {
        let budget = (target - emulator.icount()).min(250_000) as u32;
        let snapshot = emulator.step_chunk(budget);
        assert!(
            snapshot.status.error.is_none(),
            "{:?}",
            snapshot.status.error
        );
    }
    let cpu = (thread_cpu_ns() - t0) as f64 / 1e9;
    let emulated = (emulator.icount() - start) as f64 / 132e6;
    println!(
        "{emulated:.2} emulated s, {cpu:.2} CPU s -> {:.3} CPU s per emulated s; digest {}",
        cpu / emulated,
        emulator.state_digest().expect("digest")
    );
}
