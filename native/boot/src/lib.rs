//! Portable bounded Oracle boot diagnostic runtime.

#[cfg(target_arch = "wasm32")]
mod abi;
pub mod card_file;
mod capture;
#[cfg(feature = "cf-profile")]
pub mod cfprof;
mod common;
mod cputime;
pub mod dsp_patch;
pub mod dsp_stream;
#[cfg(feature = "play")]
pub mod pcm_play;
mod ram_clear;
mod runtime;
#[cfg(feature = "sharc")]
pub mod sharc_peer;
mod softfloat;
pub mod spikelog;
mod telemetry;
pub mod wav;

pub use capture::Dspi2Capture;
pub use cputime::thread_cpu_ns;
pub use telemetry::DiagnosticReport;

pub use runtime::{Emulator, Snapshot, Status};
pub use softfloat::ExecutionPolicy;
