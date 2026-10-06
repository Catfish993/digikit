use emmc_card::{
    Card, CardCheckpoint, CardError, CardIdentity, CardOverlaySector, DEFAULT_CAPACITY_BLOCKS,
    MAX_TRANSFER_BYTES, PLUSDRIVE_IMAGE_BLOCKS, RandomAccessRead, SECTOR_SIZE,
    SLC32G_CAPACITY_BLOCKS, SMALL_CAPACITY_BLOCKS,
};
use periph::{
    edma::TcdSnapshot,
    esdhc::{self, DmaBuffers, Esdhc, transfer_dma59},
};
use std::collections::BTreeMap;

struct TinyBacking(Vec<u8>);

#[test]
fn card_media_flows_through_bounded_dma59_in_both_directions() {
    let media: Vec<u8> = (0..1024).map(|n| (n % 251) as u8).collect();
    let mut card =
        Card::with_backing(CardIdentity::Mlc4G, Some(Box::new(TinyBacking(media)))).unwrap();
    let mut tcd = TcdSnapshot {
        saddr: 0x8000_0000,
        attr: 0,
        soff: 512,
        nbytes: 512,
        slast: -512,
        daddr: 0x8000_0000,
        citer: 1,
        doff: 0,
        dlast: 0,
        biter: 1,
        csr: 0,
    };
    let mut guest = [0u8; 512];
    let mut read_window = card.data_for(18, 1, 512).unwrap().unwrap();
    let read = transfer_dma59(
        18,
        tcd,
        &mut DmaBuffers {
            guest_base: 0x8000_0000,
            guest: &mut guest,
            card: &mut read_window,
        },
    )
    .unwrap();
    assert_eq!(guest.as_slice(), read_window.as_slice());
    assert_eq!(read.bytes, 512);
    assert_eq!(read.tcd.daddr, 0x8000_0200);

    // Card contents are only committed after the transfer succeeds. CMD25
    // reads the guest's own window and returns a bounded staging buffer.
    guest[0] = 0x5a;
    tcd.saddr = 0x8000_0000;
    let mut write_window = [0u8; 512];
    let written = transfer_dma59(
        25,
        tcd,
        &mut DmaBuffers {
            guest_base: 0x8000_0000,
            guest: &mut guest,
            card: &mut write_window,
        },
    )
    .unwrap();
    card.write_data(25, 1, &write_window).unwrap();
    assert_eq!(written.tcd.saddr, 0x8000_0000);
    assert_eq!(card.data_for(18, 1, 512).unwrap().unwrap()[0], 0x5a);
}

#[test]
fn card_port_drives_real_controller_identity_and_bus_test() {
    let mut controller = Esdhc::new(Card::default());
    assert!(controller.write(esdhc::BASE + esdhc::XFERTYP, 4, 0x0100_0000));
    assert_eq!(
        controller.read(esdhc::BASE + esdhc::CMDRSP0, 4),
        Some(0xc0ff_8080)
    );

    assert!(controller.write(esdhc::BASE + esdhc::XFERTYP, 4, 0x0200_0000));
    assert_eq!(
        controller.read(esdhc::BASE + esdhc::CMDRSP1, 4),
        Some(0x3332_0000)
    );
    assert_eq!(
        controller.read(esdhc::BASE + esdhc::CMDRSP2, 4),
        Some(0x5458_3239)
    );

    assert!(controller.write(esdhc::BASE + esdhc::DATPORT, 4, 0x5a));
    assert!(controller.write(esdhc::BASE + esdhc::XFERTYP, 4, 0x0e3a_0010));
    assert_eq!(
        controller.read(esdhc::BASE + esdhc::DATPORT, 4),
        Some(0xffff_ffa5)
    );
}

impl RandomAccessRead for TinyBacking {
    fn len(&self) -> u64 {
        self.0.len() as u64
    }

    fn read_at(&self, offset: u64, destination: &mut [u8]) -> usize {
        let source = self.0.get(offset as usize..).unwrap_or_default();
        let count = source.len().min(destination.len());
        destination[..count].copy_from_slice(&source[..count]);
        count
    }
}

#[test]
fn identity_commands_match_oracle() {
    let mut card = Card::default();
    assert_eq!(card.command(0, 0), [0; 4]);
    assert_eq!(card.command(1, 0), [0xc0ff_8080, 0, 0, 0]);
    assert_eq!(
        card.command(2, 0),
        [0, 0x3332_0000, 0x5458_3239, 0x0070_0000]
    );
    assert_eq!(card.command(9, 0), [0, 0xafc0_0380, 0x0000_0a03, 0]);
    assert_eq!(card.command(10, 0), card.command(2, 0));
    assert_eq!(card.command(3, 0x1234_ffff), [0x900, 0, 0, 0]);
    assert_eq!(card.rca(), 0x1234);
    assert_eq!(card.command(7, 0x1234_0000), [0x900, 0, 0, 0]);
    assert!(card.selected());
    assert_eq!(card.command(7, 0x9999_0000), [0x900, 0, 0, 0]);
    assert!(!card.selected());
    card.command(7, 0x1234_0000);
    card.command(0, 0);
    assert!(!card.selected());
    assert_eq!(card.command(42, 0), [0x900, 0, 0, 0]);
}

#[test]
fn cid_and_ext_csd_bytes_match_python_oracle_literals() {
    let mut card = Card::new(CardIdentity::Mlc4G).unwrap();
    let cid_bytes = card.command(2, 0).map(u32::to_be_bytes).concat();
    assert_eq!(
        cid_bytes,
        [
            0x00, 0x00, 0x00, 0x00, 0x45, 0x30, 0x00, 0x00, 0x30, 0x30, 0x34, 0x47, 0x00, 0x11,
            0x00, 0x00,
        ]
    );

    let Some(ext) = card.data_for(8, 0, 512).expect("CMD8 is bounded") else {
        panic!("CMD8 must return EXT_CSD");
    };
    let mut expected = [0_u8; 512];
    expected[0x9c..0x9f].copy_from_slice(&[0x00, 0x01, 0xd8]);
    expected[0xaf] = 0x01;
    expected[0xb7] = 0x01;
    expected[0xb9] = 0x01;
    expected[0xd4..0xd8].copy_from_slice(&[0x00, 0x76, 0x00, 0x00]);
    expected[0xde] = 0x01;
    expected[0xe3] = 0x08;
    assert_eq!(ext, expected);

    let small = Card::new(CardIdentity::Small).expect("small identity is supported");
    let Some(small_ext) = small.data_for(8, 0, 512).expect("CMD8 is bounded") else {
        panic!("CMD8 must return EXT_CSD");
    };
    assert_eq!(small_ext[0x98], 0x01);
    assert_eq!(&small_ext[0xd4..0xd8], &[0x00, 0x00, 0x00, 0x00]);
}

#[test]
fn default_card_is_the_pslc_32g_identity() {
    assert_eq!(CardIdentity::default(), CardIdentity::Slc32G);
    let mut card = Card::default();
    assert_eq!(card.identity(), CardIdentity::Slc32G);
    assert_eq!(card.blocks(), SLC32G_CAPACITY_BLOCKS);
    let cid_bytes = card.command(2, 0).map(u32::to_be_bytes).concat();
    // RSP3 byte 1 is the manufacturer (0x70); RSP2 + RSP1[0..2] is "TX2932".
    assert_eq!(cid_bytes[13], 0x70);
    let mut name = cid_bytes[8..12].to_vec();
    name.extend_from_slice(&cid_bytes[4..6]);
    assert_eq!(name, b"TX2932");

    let ext = card.data_for(8, 0, 512).unwrap().unwrap();
    assert_eq!(ext[0x98], 1);
    assert_eq!(&ext[0x9c..0x9f], &[0x00, 0x04, 0xdb]);
    assert_eq!(ext[0xde], 0x10);
    assert_eq!(ext[0xe3], 0x01);
    assert_eq!(&ext[0xd4..0xd8], &[0x02, 0xe1, 0xa0, 0x00]);
    assert_eq!((ext[0xaf], ext[0xb7], ext[0xb9]), (1, 1, 1));
}

/// One row of the firmware's eMMC whitelist (identical in DT2 1.16 and DN2 1.11).
struct WhitelistRow {
    manufacturer: u8,
    name: &'static [u8; 6],
    enh_size: u32,
    wp_group: u8,
    erase_group: u8,
    capacity_mlc: u32,
    capacity_slc: u32,
}

const WHITELIST: [WhitelistRow; 4] = [
    WhitelistRow {
        manufacturer: 0x11,
        name: b"004GE0",
        enh_size: 0x1d8,
        wp_group: 1,
        erase_group: 8,
        capacity_mlc: 0x76_0000,
        capacity_slc: 0x3b_0000,
    },
    WhitelistRow {
        manufacturer: 0x15,
        name: b"4FTE4R",
        enh_size: 0xe9,
        wp_group: 0x10,
        erase_group: 1,
        capacity_mlc: 0x74_8000,
        capacity_slc: 0x3a_4000,
    },
    WhitelistRow {
        manufacturer: 0x70,
        name: b"TX2932",
        enh_size: 0x4db,
        wp_group: 0x10,
        erase_group: 1,
        capacity_mlc: 0x3a4_a000,
        capacity_slc: 0x2e1_a000,
    },
    WhitelistRow {
        manufacturer: 0x70,
        name: b"TS0A32",
        enh_size: 0x4db,
        wp_group: 0x10,
        erase_group: 1,
        capacity_mlc: 0x3a4_a000,
        capacity_slc: 0x2e1_a000,
    },
];

/// Apply the firmware's check to a card: row by CID manufacturer + name, then
/// the packed EXT_CSD fields, then the capacity chosen by byte 0x98
/// (0 selects the MLC constant, 1 the SLC one, other values skip the compare).
fn whitelist_accepts(card: &mut Card) -> bool {
    let cid = card.command(2, 0).map(u32::to_be_bytes).concat();
    let manufacturer = cid[13];
    let mut name = cid[8..12].to_vec();
    name.extend_from_slice(&cid[4..6]);
    let ext = card.data_for(8, 0, 512).unwrap().unwrap();
    let Some(row) = WHITELIST
        .iter()
        .find(|row| row.manufacturer == manufacturer && row.name.as_slice() == name)
    else {
        return false;
    };
    let enh = u32::from_be_bytes([0, ext[0x9c], ext[0x9d], ext[0x9e]]);
    if enh != row.enh_size || ext[0xde] != row.wp_group || ext[0xe3] != row.erase_group {
        return false;
    }
    // The firmware derives capacity from SEC_COUNT, else from the CSD fallback
    // (C_SIZE 3775, C_SIZE_MULT 7, READ_BL_LEN 10: 0x3b0000 sectors).
    let sec_count = u32::from_be_bytes([ext[0xd4], ext[0xd5], ext[0xd6], ext[0xd7]]);
    let capacity = if sec_count != 0 { sec_count } else { 0x3b_0000 };
    assert_eq!(capacity, card.blocks());
    match ext[0x98] {
        0 => capacity == row.capacity_mlc,
        1 => capacity == row.capacity_slc,
        _ => true,
    }
}

#[test]
fn every_identity_passes_the_firmware_whitelist_check() {
    for identity in [
        CardIdentity::Slc32G,
        CardIdentity::Mlc4G,
        CardIdentity::Small,
    ] {
        let mut card = Card::new(identity).unwrap();
        assert!(whitelist_accepts(&mut card), "{identity:?}");
    }
    // The check is not vacuous: a wrong capacity or field is rejected.
    let card = Card::new(CardIdentity::Slc32G).unwrap();
    assert_eq!(card.blocks(), 0x2e1_a000);
    assert!(card.blocks() > 0x5ee180, "+Drive layout must fit");
    assert!(PLUSDRIVE_IMAGE_BLOCKS <= card.blocks());
}

#[test]
fn identity_names_and_blocks_round_trip() {
    for identity in [
        CardIdentity::Slc32G,
        CardIdentity::Mlc4G,
        CardIdentity::Small,
    ] {
        assert_eq!(CardIdentity::parse(identity.name()), Some(identity));
        assert_eq!(CardIdentity::from_blocks(identity.blocks()), Some(identity));
    }
    assert_eq!(CardIdentity::parse("bogus"), None);
    assert_eq!(CardIdentity::from_blocks(4), None);
}

#[test]
fn short_backing_reads_zeroes_and_writes_past_it_work() {
    let mut card = Card::with_backing(
        CardIdentity::Slc32G,
        Some(Box::new(TinyBacking(vec![7; SECTOR_SIZE]))),
    )
    .unwrap();
    assert_eq!(card.blocks(), SLC32G_CAPACITY_BLOCKS);
    assert_eq!(card.data_for(18, 0, 2).unwrap().unwrap(), [7, 7]);
    assert_eq!(card.data_for(18, 1, 4).unwrap().unwrap(), [0; 4]);
    let far = PLUSDRIVE_IMAGE_BLOCKS + 5;
    assert_eq!(card.data_for(18, far, 4).unwrap().unwrap(), [0; 4]);
    card.write_data(25, far, &[1, 2, 3]).unwrap();
    assert_eq!(card.data_for(18, far, 4).unwrap().unwrap(), [1, 2, 3, 0]);
}

#[test]
fn backed_reads_sparse_overlay_and_out_of_range_match_oracle() {
    let mut card = Card::with_backing(
        CardIdentity::Mlc4G,
        Some(Box::new(TinyBacking(vec![1, 2, 3]))),
    )
    .expect("default capacity is supported");
    assert_eq!(
        card.data_for(18, 0, 5).expect("bounded CMD18"),
        Some(vec![1, 2, 3, 0, 0])
    );
    assert_eq!(
        card.data_for(18, 1, 4).expect("bounded CMD18"),
        Some(vec![0; 4])
    );

    card.write_data(25, 0, &[9, 8, 7, 6])
        .expect("bounded CMD25");
    card.write_data(25, 1, &[4]).expect("bounded CMD25");
    assert_eq!(card.overlay_len(), 5);
    assert_eq!(
        card.data_for(18, 0, 5).expect("bounded CMD18"),
        Some(vec![9, 8, 7, 6, 0])
    );

    let mut reusable = [0xff; 2];
    card.read_into(1, &mut reusable)
        .expect("bounded stream read");
    assert_eq!(reusable, [4, 0]);
    card.write_data(24, 0, &[0]).expect("non-CMD25 is ignored");
    assert_eq!(
        card.data_for(17, 0, 1).expect("bounded unknown command"),
        None
    );
}

#[test]
fn overlapping_partial_and_full_sectors_preserve_backing_and_zero_writes() {
    let base: Vec<u8> = (0..8192).map(|n| (n % 251) as u8).collect();
    let mut card = Card::with_backing(
        CardIdentity::Mlc4G,
        Some(Box::new(TinyBacking(base.clone()))),
    )
    .unwrap();
    let mut expected = base;
    let mut covered = [false; 8192];
    for i in 0..130 {
        let sector = (i * 7) % 13;
        let count = (i * 37) % 1023 + 1;
        let data: Vec<u8> = (0..count).map(|j| ((i + j * 13) % 256) as u8).collect();
        let offset = sector * 512;
        card.write_data(25, sector as u32, &data).unwrap();
        expected[offset..offset + count].copy_from_slice(&data);
        covered[offset..offset + count].fill(true);
        let mut output = [0; 1017];
        card.read_into(((i + 3) % 13) as u32, &mut output).unwrap();
        let start = ((i + 3) % 13) * 512;
        assert_eq!(output.as_slice(), &expected[start..start + output.len()]);
    }
    assert_eq!(card.overlay_len(), covered.iter().filter(|&&b| b).count());
}

#[test]
fn restore_python_byte_overlay_and_card_identity_atomically() {
    let mut card = Card::with_backing(
        CardIdentity::Mlc4G,
        Some(Box::new(TinyBacking(vec![0xa5; 1024]))),
    )
    .unwrap();
    card.write_data(25, 0, &[0x13]).unwrap();
    let checkpoint = CardCheckpoint {
        blocks: DEFAULT_CAPACITY_BLOCKS,
        rca: 0x1234,
        selected: true,
        overlay: BTreeMap::from([(0, 0), (513, 0x5a)]),
    };
    let mut wrong_capacity = checkpoint.clone();
    wrong_capacity.blocks = SMALL_CAPACITY_BLOCKS;
    assert_eq!(
        card.restore_checkpoint(&wrong_capacity),
        Err(CardError::CheckpointCapacityMismatch {
            expected: DEFAULT_CAPACITY_BLOCKS,
            actual: SMALL_CAPACITY_BLOCKS,
        })
    );
    assert_eq!(card.overlay_len(), 1);
    assert!(!card.selected());
    card.restore_checkpoint(&checkpoint).unwrap();
    assert_eq!(card.rca(), 0x1234);
    assert!(card.selected());
    assert_eq!(card.overlay_len(), 2);
    assert_eq!(card.data_for(18, 0, 2).unwrap().unwrap(), [0, 0xa5]);
    assert_eq!(
        card.data_for(18, 1, 3).unwrap().unwrap(),
        [0xa5, 0x5a, 0xa5]
    );
}

fn sector_record(sector: u64, bytes: &[(usize, u8)]) -> CardOverlaySector {
    let mut record = CardOverlaySector {
        sector,
        written: [0; SECTOR_SIZE / 8],
        data: [0; SECTOR_SIZE],
    };
    for &(byte, value) in bytes {
        record.written[byte >> 3] |= 1 << (byte & 7);
        record.data[byte] = value;
    }
    record
}

#[test]
fn restore_sector_overlay_preserves_partial_zero_masks_and_full_sectors() {
    let mut card = Card::with_backing(
        CardIdentity::Mlc4G,
        Some(Box::new(TinyBacking(vec![0xa5; SECTOR_SIZE * 3]))),
    )
    .unwrap();
    let state = CardCheckpoint {
        blocks: DEFAULT_CAPACITY_BLOCKS,
        rca: 0x4321,
        selected: true,
        overlay: BTreeMap::new(),
    };
    let mut full = sector_record(1, &[]);
    full.written.fill(0xff);
    full.data.fill(0x3c);

    card.restore_checkpoint_sectors(&state, &[sector_record(0, &[(0, 0), (9, 0x5a)]), full])
        .unwrap();

    assert_eq!(card.rca(), 0x4321);
    assert!(card.selected());
    assert_eq!(card.overlay_len(), SECTOR_SIZE + 2);
    let mut partial = [0; 16];
    card.read_into(0, &mut partial).unwrap();
    assert_eq!(partial[0], 0);
    assert_eq!(partial[9], 0x5a);
    assert!(
        partial
            .iter()
            .enumerate()
            .filter(|(byte, _)| *byte != 0 && *byte != 9)
            .all(|(_, &value)| value == 0xa5)
    );
    assert_eq!(
        card.data_for(18, 1, SECTOR_SIZE).unwrap().unwrap(),
        vec![0x3c; SECTOR_SIZE]
    );
}

#[test]
fn restore_sector_overlay_rejects_invalid_inputs_without_mutation() {
    let mut card = Card::with_backing(
        CardIdentity::Mlc4G,
        Some(Box::new(TinyBacking(vec![0xa5; SECTOR_SIZE]))),
    )
    .unwrap();
    card.write_data(25, 0, &[0x13]).unwrap();
    card.command(3, 0x1234_0000);
    card.command(7, 0x1234_0000);
    let state = CardCheckpoint {
        blocks: DEFAULT_CAPACITY_BLOCKS,
        rca: 0x4321,
        selected: false,
        overlay: BTreeMap::new(),
    };
    let unchanged = |card: &Card| {
        assert_eq!(card.overlay_len(), 1);
        assert_eq!(card.rca(), 0x1234);
        assert!(card.selected());
        assert_eq!(card.data_for(18, 0, 1).unwrap().unwrap(), [0x13]);
    };

    assert_eq!(
        card.restore_checkpoint_sectors(
            &state,
            &[sector_record(1, &[(0, 1)]), sector_record(1, &[(1, 2)])]
        ),
        Err(CardError::OverlaySectorNotStrictlyIncreasing {
            previous: 1,
            sector: 1
        })
    );
    unchanged(&card);
    assert_eq!(
        card.restore_checkpoint_sectors(
            &state,
            &[sector_record(2, &[(0, 1)]), sector_record(1, &[(1, 2)])]
        ),
        Err(CardError::OverlaySectorNotStrictlyIncreasing {
            previous: 2,
            sector: 1
        })
    );
    unchanged(&card);
    assert_eq!(
        card.restore_checkpoint_sectors(
            &state,
            &[sector_record(u64::from(DEFAULT_CAPACITY_BLOCKS), &[(0, 1)])]
        ),
        Err(CardError::OverlaySectorOutOfRange {
            sector: u64::from(DEFAULT_CAPACITY_BLOCKS),
            blocks: DEFAULT_CAPACITY_BLOCKS
        })
    );
    unchanged(&card);
    assert_eq!(
        card.restore_checkpoint_sectors(&state, &[sector_record(1, &[])]),
        Err(CardError::OverlaySectorEmpty { sector: 1 })
    );
    unchanged(&card);
    let mut noncanonical = sector_record(1, &[(0, 1)]);
    noncanonical.data[1] = 2;
    assert_eq!(
        card.restore_checkpoint_sectors(&state, &[noncanonical]),
        Err(CardError::OverlaySectorNonCanonicalData { sector: 1, byte: 1 })
    );
    unchanged(&card);
    let mut byte_overlay_state = state.clone();
    byte_overlay_state.overlay.insert(0, 0);
    assert_eq!(
        card.restore_checkpoint_sectors(&byte_overlay_state, &[sector_record(1, &[(0, 1)])]),
        Err(CardError::CheckpointOverlayNotEmpty)
    );
    unchanged(&card);
}

#[test]
fn oversized_transfer_is_rejected_before_allocation() {
    let card = Card::default();
    assert_eq!(
        card.data_for(18, 0, MAX_TRANSFER_BYTES + 1),
        Err(CardError::TransferTooLarge {
            requested: MAX_TRANSFER_BYTES + 1,
            maximum: MAX_TRANSFER_BYTES,
        })
    );
}

#[test]
fn bus_test_and_capacity_rejection_match_oracle() {
    let card = Card::default();
    assert_eq!(card.read_word(14, 0x0000_005a), 0xffff_ffa5);
    assert_eq!(card.read_word(19, 0x5a), 0);
    assert_eq!(CardIdentity::from_blocks(4), None);
}
