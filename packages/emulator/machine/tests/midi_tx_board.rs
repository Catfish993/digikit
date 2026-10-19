use coldfire::Bus;
use emmc_card::{Card, CardIdentity};
use machine::{Board, CompletionPolicy, MIDI_UDR, SemaphoreAddresses};

fn board() -> Board {
    let mut board = Board::new(
        Card::new(CardIdentity::Mlc4G).unwrap(),
        SemaphoreAddresses::default(),
        CompletionPolicy::Oracle,
    );
    board.map_ram_page(MIDI_UDR & !0xf_ffff).unwrap();
    board
}

#[test]
fn byte_stores_to_the_uart9_data_register_are_recorded_in_order() {
    let mut board = board();
    for byte in [0xf0u8, 0x00, 0x20, 0xf7] {
        board.write8(MIDI_UDR, byte).unwrap();
    }
    assert_eq!(board.take_midi_tx(), vec![0xf0, 0x00, 0x20, 0xf7]);
    assert!(board.take_midi_tx().is_empty());
}

#[test]
fn other_uart9_registers_and_wide_stores_are_not_midi_output() {
    let mut board = board();
    board.write8(MIDI_UDR + 1, 0x55).unwrap();
    board.write8(MIDI_UDR - 8, 0x66).unwrap();
    board.write32(MIDI_UDR, 0x1122_3344).unwrap();
    assert!(board.take_midi_tx().is_empty());
}
