//! Which generated DSP core runs a firmware's DSP.
//!
//! A build holds one generated core (`SHARC_GEN_DIR`), or two with the `second-core` feature
//! (`SHARC_GEN_DIR_2` is the second one's slot; the names do not say which device a core is for).
//! Each core knows the DSP image it was made for (`dn2-1.11`, `dt2-1.16`), and the core whose image
//! name starts with the firmware's device id runs that firmware's DSP. A device without a core of
//! its own gets the first core, as a single-core build always did (the image's blocks then do not
//! match and the engine interprets).

use std::{cell::RefCell, rc::Rc};

use elektron_native_boot::{
    dsp_patch::DspPatch,
    sharc_peer::{IdleLoop, Shared, ThreadedHandle, ThreadedPeer, open_engine_patched},
};

/// The generated core a DSP session runs on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Core {
    First,
    #[cfg(feature = "second-core")]
    Second,
}

impl Core {
    /// The core for a device id (`dn2`, `dt2`): the one made for that device's image.
    pub fn for_device(device: &str) -> Self {
        #[cfg(feature = "second-core")]
        if !sharc_native::gen_image_name().starts_with(device)
            && sharc_native_2::gen_image_name().starts_with(device)
        {
            return Self::Second;
        }
        let _ = device;
        Self::First
    }

    /// The name of the DSP image the core was made for.
    pub fn image_name(self) -> &'static str {
        match self {
            Self::First => sharc_native::gen_image_name(),
            #[cfg(feature = "second-core")]
            Self::Second => sharc_native_2::gen_image_name(),
        }
    }
}

/// Everything `open_engine_patched` needs; it runs on the worker thread.
pub struct EngineInputs {
    pub image: Vec<u8>,
    pub state: Vec<u8>,
    pub clock_base: u64,
    pub patch: Option<DspPatch>,
    pub idle: IdleLoop,
}

/// The DSP worker of `core`, with its DSPI2 peer.
pub fn spawn_dsp(
    core: Core,
    inputs: EngineInputs,
    period: u32,
    link_timing: bool,
    fast_tier: bool,
) -> Result<(ThreadedPeer, ThreadedHandle, Rc<RefCell<Shared>>), String> {
    let EngineInputs {
        image,
        state,
        clock_base,
        patch,
        idle,
    } = inputs;
    match core {
        Core::First => {
            if fast_tier {
                sharc_fast_cl::register();
            }
            ThreadedPeer::spawn_with_timing(
                move || {
                    open_engine_patched(&image, &state, clock_base, patch.as_ref(), idle)
                        .map(elektron_native_boot::sharc_peer::NativeDsp)
                },
                period,
                idle.range,
                link_timing,
            )
        }
        #[cfg(feature = "second-core")]
        Core::Second => {
            if fast_tier {
                sharc_fast_cl_2::register();
            }
            ThreadedPeer::spawn_with_timing(
                move || {
                    elektron_native_boot::sharc_peer::second::open_engine_patched(
                        &image,
                        &state,
                        clock_base,
                        patch.as_ref(),
                        idle,
                    )
                    .map(elektron_native_boot::sharc_peer::second::NativeDsp)
                },
                period,
                idle.range,
                link_timing,
            )
        }
    }
}
