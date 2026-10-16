//! patchkit: firmware modules for Digitakt II and Digitone II.
//!
//! One implementation for the command line and the browser (`wasm32`). It does everything at patch time:
//! `.syx` decode and encode, aPLib pack, HMAC seal, port lockfiles, linking, apply, verify. Toolchains
//! and firmware databases are used only when modules and ports are built (Python, `tools/patchkit/pk_*.py`).

#[macro_use]
pub mod error;
pub mod aplib;
pub mod firmware;
pub mod syx;

pub use error::{Error, Result};
pub mod apply;
pub mod bootrom;
pub mod dko;
pub mod dkm;
pub mod dsp;
pub mod explain;
pub mod expr;
pub mod link;
pub mod modgen;
pub mod module;
pub mod port;
pub mod prim;
pub mod record;
pub mod register;
pub mod typed;
pub mod verify;
pub mod wasm;
