//! The project seed of a verified DT2 1.16 firmware image.

use device_profile::{PlusdriveProjectContract, Registry};
use sha2::{Digest, Sha256};

use crate::ProjectSeed;

/// The MAIN section and the +Drive project contract of a DT2 .syx.
#[derive(Clone, Debug)]
pub struct Dt2Seed {
    main: Vec<u8>,
    contract: PlusdriveProjectContract,
}

impl Dt2Seed {
    /// Parse `syx`, find MAIN, and check it against the embedded device
    /// profiles. Fails for anything but a DT2 firmware with a project contract.
    pub fn from_syx(syx: &[u8]) -> Result<Self, String> {
        let digest = Sha256::digest(syx)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let main = dt2_firmware_loader::parse(syx)
            .map_err(|e| e.to_string())?
            .sections
            .into_iter()
            .find(|section| section.id == 3)
            .ok_or("firmware has no MAIN section")?
            .bytes;
        let registry = Registry::load().map_err(|e| e.to_string())?;
        let (device, profile, _) = registry
            .boot_for_main(&digest, &main)
            .map_err(|e| e.to_string())?;
        if device.short != "dt2" {
            return Err("project seeding is DT2-only".into());
        }
        let contract = profile
            .plusdrive_project_contract
            .ok_or("firmware has no +Drive project contract")?;
        Ok(Self { main, contract })
    }

    pub fn seed(&self) -> ProjectSeed<'_> {
        ProjectSeed {
            main: &self.main,
            contract: self.contract,
        }
    }
}
