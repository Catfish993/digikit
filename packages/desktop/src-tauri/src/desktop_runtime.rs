//! The desktop core is the shared coupled runtime (`native/coupled`, also used by
//! the headless `coupled-run`). Configured local DN2 ready assets attach coupled
//! audio on normal startup. The fixture gates stay here, as thin tests, so their
//! names and asserted hashes keep their place for the tooling that runs them.
pub use digi_coupled::{AudioOptions, CoupledRuntime as DesktopRuntime, STEP_CHUNK};

#[cfg(feature = "coupled-audio")]
mod coupled {
    #[cfg(test)]
    mod tests {
        use digi_coupled::gates;

        #[test]
        #[ignore = "private DN2 ready fixtures and locally generated DSP; bounded 100M-instruction gate"]
        fn coupled_ready_exactness() {
            gates::coupled_ready_exactness();
        }

        #[test]
        #[ignore = "private DN2 ready fixtures; captures the desktop Audition Trig 1 path without the shared QA script"]
        fn desktop_trig1_capture_without_qa_script() {
            gates::desktop_trig1_capture_without_qa_script();
        }

        #[test]
        #[ignore = "private DN2 fixtures; missing-device path must leave coupling intact"]
        fn missing_device_keeps_coupled_runtime_and_records_error() {
            gates::missing_device_keeps_coupled_runtime_and_records_error();
        }

        #[test]
        #[ignore = "private DN2 fixtures and an audible default output device"]
        fn default_output_receives_nonzero_emulated_pcm_and_tears_down() {
            gates::default_output_receives_nonzero_emulated_pcm_and_tears_down();
        }
    }
}
