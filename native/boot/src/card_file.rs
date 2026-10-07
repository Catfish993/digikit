//! A card backed by an image file (the +Drive images `native/plusdrive`
//! builds), for hosts that need no read-error reporting: the coupled runner,
//! the ready-state tool and the examples.

use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
    sync::Mutex,
};

use emmc_card::{Card, CardIdentity, RandomAccessRead};

struct FileBacking {
    file: Mutex<File>,
    len: u64,
}

impl RandomAccessRead for FileBacking {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_at(&self, offset: u64, destination: &mut [u8]) -> usize {
        let Ok(mut file) = self.file.lock() else {
            return 0;
        };
        match file
            .seek(SeekFrom::Start(offset))
            .and_then(|_| file.read_exact(destination))
        {
            Ok(()) => destination.len(),
            Err(_) => 0,
        }
    }
}

/// A card whose base media is the 512-byte-aligned image at `path`, read on
/// demand. The identity comes from `DIGI_CARD_IDENTITY` (default
/// [`CardIdentity::Slc32G`]); the image may not exceed its capacity.
pub fn card_from_image(path: &Path) -> Result<Card, String> {
    let metadata = std::fs::metadata(path).map_err(|e| format!("card image: {e}"))?;
    if !metadata.is_file() || metadata.len() % 512 != 0 {
        return Err("card image must be a regular 512-byte aligned file".into());
    }
    let identity = CardIdentity::from_env()?;
    let blocks = u32::try_from(metadata.len() / 512).map_err(|_| "card image is too large")?;
    if blocks > identity.blocks() {
        return Err(format!(
            "card image has {blocks:#x} sectors, more than the {} identity's {:#x}",
            identity.name(),
            identity.blocks()
        ));
    }
    let backing = FileBacking {
        file: Mutex::new(File::open(path).map_err(|e| format!("card image: {e}"))?),
        len: metadata.len(),
    };
    Card::with_backing(identity, Some(Box::new(backing))).map_err(|e| format!("card: {e:?}"))
}
