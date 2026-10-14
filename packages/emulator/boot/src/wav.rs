//! Minimal streaming WAV writer: 32-bit float, interleaved. The header sizes
//! are patched as it goes, so a capture cut short by a kill still opens.

use std::fs::File;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

/// Patch the header about once per second of 48 kHz stereo.
const PATCH_EVERY_BYTES: u64 = 48_000 * 2 * 4;

pub struct WavWriter {
    out: BufWriter<File>,
    data_bytes: u64,
    since_patch: u64,
}

impl WavWriter {
    pub fn create(path: &Path, sample_rate: u32, channels: u16) -> io::Result<Self> {
        let mut out = BufWriter::new(File::create(path)?);
        let block = u32::from(channels) * 4;
        out.write_all(b"RIFF")?;
        out.write_all(&36u32.to_le_bytes())?;
        out.write_all(b"WAVEfmt ")?;
        out.write_all(&16u32.to_le_bytes())?;
        out.write_all(&3u16.to_le_bytes())?; // WAVE_FORMAT_IEEE_FLOAT
        out.write_all(&channels.to_le_bytes())?;
        out.write_all(&sample_rate.to_le_bytes())?;
        out.write_all(&(sample_rate * block).to_le_bytes())?;
        out.write_all(&(block as u16).to_le_bytes())?;
        out.write_all(&32u16.to_le_bytes())?;
        out.write_all(b"data")?;
        out.write_all(&0u32.to_le_bytes())?;
        Ok(Self {
            out,
            data_bytes: 0,
            since_patch: 0,
        })
    }

    /// Bytes of sample data written so far.
    pub fn data_bytes(&self) -> u64 {
        self.data_bytes
    }

    pub fn write(&mut self, samples: &[f32]) -> io::Result<()> {
        for s in samples {
            self.out.write_all(&s.to_le_bytes())?;
        }
        let n = samples.len() as u64 * 4;
        self.data_bytes += n;
        self.since_patch += n;
        if self.since_patch >= PATCH_EVERY_BYTES {
            self.since_patch = 0;
            self.patch()?;
        }
        Ok(())
    }

    /// Write the RIFF and data sizes for what has been written, and flush.
    pub fn patch(&mut self) -> io::Result<()> {
        let data = u32::try_from(self.data_bytes).unwrap_or(u32::MAX - 36);
        self.out.flush()?;
        let file = self.out.get_mut();
        file.seek(SeekFrom::Start(4))?;
        file.write_all(&data.saturating_add(36).to_le_bytes())?;
        file.seek(SeekFrom::Start(40))?;
        file.write_all(&data.to_le_bytes())?;
        file.seek(SeekFrom::End(0))?;
        Ok(())
    }
}

impl Drop for WavWriter {
    fn drop(&mut self) {
        let _ = self.patch();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_sizes_match_the_samples_written() {
        let path = std::env::temp_dir().join(format!("wav-test-{}.wav", std::process::id()));
        {
            let mut w = WavWriter::create(&path, 48_000, 2).unwrap();
            w.write(&[0.0, 0.5, -0.5, 1.0, 0.25, -0.25]).unwrap();
            assert_eq!(w.data_bytes(), 24);
        }
        let bytes = std::fs::read(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(bytes.len(), 44 + 24);
        assert_eq!(&bytes[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(bytes[4..8].try_into().unwrap()), 60);
        assert_eq!(u16::from_le_bytes(bytes[20..22].try_into().unwrap()), 3);
        assert_eq!(
            u32::from_le_bytes(bytes[24..28].try_into().unwrap()),
            48_000
        );
        assert_eq!(u32::from_le_bytes(bytes[40..44].try_into().unwrap()), 24);
        assert_eq!(f32::from_le_bytes(bytes[48..52].try_into().unwrap()), 0.5);
    }
}
