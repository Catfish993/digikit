//! DT2 1.16 built-in-project extraction and reference replacement.

use device_profile::PlusdriveProjectContract;
use dt2_firmware_loader::depack_section;

use crate::build_project_header;

pub const PROJECT_RECORD_SIZE: usize = 0x00dd_9714;
const PROJECT_HEADER_SIZE: usize = 0x110;
const BUILTIN_STREAM_OFFSET: usize = 0x25edd8;
const MAX_DEPACKED_BYTES: usize = 0x00c3_f424;
const CONTAINER_SIZE: usize = 0x00c3_f424;
const CONTAINER_MAGIC: u32 = 0xbeef_bace;
const CONTAINER_END_OFFSET: usize = 0x00c3_e400;
const CONTAINER_END_MAGIC: u32 = 0xbace_f00c;
const REF_TABLE_OFFSET: usize = 0x00c2_d6fb;
const REF_COUNT: usize = 1024;
const KITS_OFFSET: usize = 0x00ae_0200;
const KIT_SIZE: usize = 0x2800;
const KIT_HEADER: usize = 0x3c;
const TRACK_SIZE: usize = 0x155;
const TRACK_SLOT: usize = 0x64;
const TRACK_REF: usize = 0x129;
/// Kit 0 of the built-in project is a factory demo kit; kit 32 is an
/// untouched default kit, which a card-less boot's kit matches.
const ACTIVE_KIT: usize = 0;
const DEFAULT_KIT: usize = 32;

#[derive(Clone, Copy, Debug)]
pub struct ProjectSeed<'a> {
    pub main: &'a [u8],
    pub contract: PlusdriveProjectContract,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectReport {
    pub table_refs: usize,
    pub extra_refs: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProjectError {
    UnsupportedContract,
    StreamOutOfBounds,
    InvalidStreamHeader,
    InvalidStreamChecksum,
    Depack(String),
    InvalidContainer,
    InvalidReference,
    OutputOverflow,
}

fn word(bytes: &[u8], at: usize) -> Option<u32> {
    bytes
        .get(at..at + 4)
        .and_then(|part| part.try_into().ok())
        .map(u32::from_be_bytes)
}

fn extract(seed: ProjectSeed<'_>) -> Result<Vec<u8>, ProjectError> {
    if seed.contract != PlusdriveProjectContract::Dt2V3Default116 {
        return Err(ProjectError::UnsupportedContract);
    }
    let stream = seed
        .main
        .get(BUILTIN_STREAM_OFFSET..)
        .ok_or(ProjectError::StreamOutOfBounds)?;
    let length = word(stream, 0).ok_or(ProjectError::InvalidStreamHeader)? as usize;
    let sum = word(stream, 4).ok_or(ProjectError::InvalidStreamHeader)?;
    let end = 8usize
        .checked_add(length)
        .ok_or(ProjectError::InvalidStreamHeader)?;
    let payload = stream.get(8..end).ok_or(ProjectError::StreamOutOfBounds)?;
    if payload
        .iter()
        .fold(0u32, |total, byte| total.wrapping_add(u32::from(*byte)))
        != sum
    {
        return Err(ProjectError::InvalidStreamChecksum);
    }
    let container = depack_section(stream, BUILTIN_STREAM_OFFSET, MAX_DEPACKED_BYTES)
        .map_err(|error| ProjectError::Depack(error.to_string()))?;
    validate_container(&container)?;
    Ok(container)
}

fn validate_container(container: &[u8]) -> Result<(), ProjectError> {
    if container.len() != CONTAINER_SIZE
        || word(&container, 0) != Some(CONTAINER_MAGIC)
        || word(&container, 4) != Some(3)
        || word(&container, CONTAINER_END_OFFSET) != Some(CONTAINER_END_MAGIC)
    {
        return Err(ProjectError::InvalidContainer);
    }
    Ok(())
}

fn nonempty_refs(container: &[u8]) -> Result<Vec<[u8; 16]>, ProjectError> {
    let end = REF_TABLE_OFFSET
        .checked_add(REF_COUNT * 16)
        .ok_or(ProjectError::InvalidContainer)?;
    let table = container
        .get(REF_TABLE_OFFSET..end)
        .ok_or(ProjectError::InvalidContainer)?;
    let mut refs = Vec::new();
    for reference in table.chunks_exact(16) {
        let reference: [u8; 16] = reference.try_into().unwrap();
        if reference != [0; 16]
            && reference != [0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        {
            refs.push(reference);
        }
    }
    Ok(refs)
}

fn point_refs_at(
    mut container: Vec<u8>,
    reference: [u8; 16],
) -> Result<(Vec<u8>, ProjectReport), ProjectError> {
    let old = nonempty_refs(&container)?;
    if old.is_empty() {
        return Ok((
            container,
            ProjectReport {
                table_refs: 0,
                extra_refs: 0,
            },
        ));
    }
    let table_end = REF_TABLE_OFFSET + REF_COUNT * 16;
    let mut table_refs = 0;
    for slot in 0..REF_COUNT {
        let at = REF_TABLE_OFFSET + slot * 16;
        let current: [u8; 16] = container[at..at + 16].try_into().unwrap();
        if current != [0; 16]
            && current != [0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        {
            container[at..at + 16].copy_from_slice(&reference);
            table_refs += 1;
        }
    }
    let mut distinct = old;
    distinct.sort_unstable();
    distinct.dedup();
    let mut copies = 0;
    for old_ref in distinct {
        let mut at = 0;
        while let Some(found) = container[at..]
            .windows(16)
            .position(|candidate| candidate == old_ref)
        {
            let position = at + found;
            container[position..position + 16].copy_from_slice(&reference);
            copies += 1;
            at = position + 16;
        }
    }
    debug_assert!(table_end <= container.len());
    Ok((
        container,
        ProjectReport {
            table_refs,
            extra_refs: copies,
        },
    ))
}

/// Replaces the active kit (after its header) with the default kit, keeping
/// every track's sample slot and reference copy. Run after `point_refs_at`.
fn reset_active_kit(container: &mut [u8]) {
    let dst = KITS_OFFSET + ACTIVE_KIT * KIT_SIZE;
    let src = KITS_OFFSET + DEFAULT_KIT * KIT_SIZE;
    let mut keep: Vec<(usize, Vec<u8>)> = Vec::new();
    for track in 0..16 {
        let at = KIT_HEADER + track * TRACK_SIZE;
        for (field, len) in [(TRACK_SLOT, 2), (TRACK_REF, 16)] {
            let from = dst + at + field;
            keep.push((from, container[from..from + len].to_vec()));
        }
    }
    container.copy_within(src + KIT_HEADER..src + KIT_SIZE, dst + KIT_HEADER);
    for (at, data) in keep {
        container[at..at + data.len()].copy_from_slice(&data);
    }
}

pub fn build_project_record(
    seed: ProjectSeed<'_>,
    reference: [u8; 16],
    sequence: u32,
) -> Result<(Vec<u8>, ProjectReport), ProjectError> {
    if reference == [0; 16] {
        return Err(ProjectError::InvalidReference);
    }
    let source = extract(seed)?;
    let (mut container, report) = point_refs_at(source, reference)?;
    reset_active_kit(&mut container);
    let mut record = build_project_header(PROJECT_HEADER_SIZE, sequence, 4)
        .map_err(|_| ProjectError::OutputOverflow)?;
    record.extend_from_slice(&container);
    if record.len() > PROJECT_RECORD_SIZE {
        return Err(ProjectError::OutputOverflow);
    }
    record.resize(PROJECT_RECORD_SIZE, 0);
    Ok((record, report))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_streams_fail_before_depacking() {
        let seed = ProjectSeed {
            main: &[0; BUILTIN_STREAM_OFFSET + 8],
            contract: PlusdriveProjectContract::Dt2V3Default116,
        };
        assert!(matches!(extract(seed), Err(ProjectError::Depack(_))));
        assert_eq!(
            extract(ProjectSeed {
                main: &[],
                contract: PlusdriveProjectContract::Dt2V3Default116
            }),
            Err(ProjectError::StreamOutOfBounds)
        );
        let mut bad_sum = vec![0; BUILTIN_STREAM_OFFSET + 9];
        bad_sum[BUILTIN_STREAM_OFFSET..BUILTIN_STREAM_OFFSET + 4]
            .copy_from_slice(&1u32.to_be_bytes());
        bad_sum[BUILTIN_STREAM_OFFSET + 8] = 1;
        assert_eq!(
            extract(ProjectSeed {
                main: &bad_sum,
                contract: PlusdriveProjectContract::Dt2V3Default116
            }),
            Err(ProjectError::InvalidStreamChecksum)
        );
    }
    #[test]
    fn validates_container_and_replaces_only_known_refs() {
        let mut container = vec![0; CONTAINER_SIZE];
        container[..4].copy_from_slice(&CONTAINER_MAGIC.to_be_bytes());
        container[4..8].copy_from_slice(&3u32.to_be_bytes());
        container[CONTAINER_END_OFFSET..CONTAINER_END_OFFSET + 4]
            .copy_from_slice(&CONTAINER_END_MAGIC.to_be_bytes());
        let old = [1u8; 16];
        container[REF_TABLE_OFFSET..REF_TABLE_OFFSET + 16].copy_from_slice(&old);
        container[100..116].copy_from_slice(&old);
        let (mut patched, report) = point_refs_at(container, [2; 16]).unwrap();
        assert_eq!(report.table_refs, 1);
        assert_eq!(report.extra_refs, 1);
        assert_eq!(&patched[100..116], &[2; 16]);
        patched[4..8].copy_from_slice(&2u32.to_be_bytes());
        assert_eq!(
            validate_container(&patched),
            Err(ProjectError::InvalidContainer)
        );
    }
}
