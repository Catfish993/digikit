//! Opt-in raw generated-block call snapshots for a bounded external
//! diagnostic.  This deliberately observes only: it neither changes guest
//! state nor retries a block.  The canonical snapshots contain overlay memory
//! and must be paired with the same base image used by the caller.

use crate::{canon, rt::*};
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::path::{Path, PathBuf};

const PC_ENV: &str = "SHARC_LIVE_AOT_CAPTURE_PC";
const DIR_ENV: &str = "SHARC_LIVE_AOT_CAPTURE_DIR";
const LIMIT_ENV: &str = "SHARC_LIVE_AOT_CAPTURE_LIMIT";
const DEFAULT_LIMIT: u32 = 4;
const MAX_LIMIT: u32 = 16;

pub struct LiveCapture {
    target_pc: u32,
    dir: PathBuf,
    limit: u32,
    captured: u32,
    disabled: bool,
}

pub struct Ticket {
    ordinal: u32,
    entry_pc: u32,
    before_icount: u64,
}

impl LiveCapture {
    /// Absent unless both target and output directory are configured.  A bad
    /// diagnostic configuration is reported once and leaves emulation alone.
    pub fn from_env() -> Option<Self> {
        let pc = std::env::var(PC_ENV).ok();
        let dir = std::env::var_os(DIR_ENV);
        if pc.is_none() && dir.is_none() {
            return None;
        }
        let Some(pc) = pc else {
            eprintln!("live AOT capture disabled: {DIR_ENV} requires {PC_ENV}");
            return None;
        };
        let Some(dir) = dir else {
            eprintln!("live AOT capture disabled: {PC_ENV} requires {DIR_ENV}");
            return None;
        };
        let target_pc = match parse_pc(&pc) {
            Ok(value) => value,
            Err(error) => {
                eprintln!("live AOT capture disabled: {PC_ENV} {error}");
                return None;
            }
        };
        let limit = match std::env::var(LIMIT_ENV) {
            Ok(value) => match value.parse::<u32>() {
                Ok(value @ 1..=MAX_LIMIT) => value,
                _ => {
                    eprintln!("live AOT capture disabled: {LIMIT_ENV} must be 1..={MAX_LIMIT}");
                    return None;
                }
            },
            Err(_) => DEFAULT_LIMIT,
        };
        let dir = PathBuf::from(dir);
        if let Err(error) = fs::create_dir_all(&dir) {
            eprintln!(
                "live AOT capture disabled: cannot create {}: {error}",
                dir.display()
            );
            return None;
        }
        Some(Self {
            target_pc,
            dir,
            limit,
            captured: 0,
            disabled: false,
        })
    }

    /// This is called after the dispatcher has prepared bank/model/chain
    /// state and the fast tier declined, immediately before `entry.f`.
    pub fn before(
        &mut self,
        entry_pc: u32,
        state: &St,
        instruction_clock: bool,
        instruction_clock_base: u64,
    ) -> Option<Ticket> {
        if self.disabled || entry_pc != self.target_pc || self.captured >= self.limit {
            return None;
        }
        self.captured += 1;
        let ticket = Ticket {
            ordinal: self.captured,
            entry_pc,
            before_icount: state.icount,
        };
        let prefix = self.prefix(ticket.ordinal);
        if let Err(error) = self.write_new(
            &prefix.with_extension("pre.dsp"),
            &canon::export_state(state, true),
        ) {
            self.fail(error);
            return None;
        }
        let mut meta = String::new();
        let _ = writeln!(meta, "phase\tfield\tindex\tvalue\tmask");
        let _ = writeln!(meta, "meta\tschema_version\t0\t1\t0");
        let _ = writeln!(meta, "meta\tcanonical_ranges\t0\t1\t0");
        let _ = writeln!(meta, "meta\tcanonical_requires_same_base_image\t0\t1\t0");
        let _ = writeln!(meta, "meta\ttarget_pc\t0\t{}\t0", self.target_pc);
        let _ = writeln!(meta, "meta\tentry_pc\t0\t{}\t0", entry_pc);
        write_state_rows(
            &mut meta,
            "pre",
            state,
            instruction_clock,
            instruction_clock_base,
        );
        if let Err(error) = self.write_new(&prefix.with_extension("tsv"), meta.as_bytes()) {
            self.fail(error);
            return None;
        }
        Some(ticket)
    }

    /// This is called immediately after `entry.f` returns, before dispatcher
    /// model, bank, chain, or trap handling.  Every return code is retained.
    pub fn after(
        &mut self,
        ticket: Ticket,
        state: &St,
        code: u32,
        instruction_clock: bool,
        instruction_clock_base: u64,
    ) {
        if self.disabled {
            return;
        }
        let prefix = self.prefix(ticket.ordinal);
        if let Err(error) = self.write_new(
            &prefix.with_extension("post.dsp"),
            &canon::export_state(state, true),
        ) {
            self.fail(error);
            return;
        }
        let mut meta = String::new();
        let _ = writeln!(meta, "post\treturn_code\t0\t{code}\t0");
        let _ = writeln!(
            meta,
            "post\ticount_delta\t0\t{}\t0",
            state.icount.saturating_sub(ticket.before_icount)
        );
        let _ = writeln!(meta, "post\tentry_pc\t0\t{}\t0", ticket.entry_pc);
        write_state_rows(
            &mut meta,
            "post",
            state,
            instruction_clock,
            instruction_clock_base,
        );
        if let Err(error) = append(&prefix.with_extension("tsv"), meta.as_bytes()) {
            self.fail(error);
        }
    }

    fn prefix(&self, ordinal: u32) -> PathBuf {
        self.dir.join(format!("aot-call-{ordinal:02}"))
    }

    fn write_new(&self, path: &Path, bytes: &[u8]) -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(|error| format!("cannot create {}: {error}", path.display()))?;
        file.write_all(bytes)
            .map_err(|error| format!("cannot write {}: {error}", path.display()))
    }

    fn fail(&mut self, error: String) {
        self.disabled = true;
        eprintln!("live AOT capture disabled after diagnostic I/O error: {error}");
    }
}

fn append(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .append(true)
        .open(path)
        .map_err(|error| format!("cannot append {}: {error}", path.display()))?;
    file.write_all(bytes)
        .map_err(|error| format!("cannot append {}: {error}", path.display()))
}

fn parse_pc(value: &str) -> Result<u32, &'static str> {
    let value = value.trim();
    let (radix, digits) = value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
        .map(|digits| (16, digits))
        .unwrap_or((10, value));
    u32::from_str_radix(digits, radix).map_err(|_| "must be a decimal or 0x hexadecimal u32")
}

fn write_state_rows(
    out: &mut String,
    phase: &str,
    state: &St,
    instruction_clock: bool,
    instruction_clock_base: u64,
) {
    let _ = writeln!(out, "{phase}\tpc_sw\t0\t{}\t0", state.pc_sw);
    let _ = writeln!(out, "{phase}\ticount\t0\t{}\t0", state.icount);
    let _ = writeln!(out, "{phase}\tlimit\t0\t{}\t0", state.limit);
    let _ = writeln!(out, "{phase}\tchain\t0\t{}\t0", state.chain);
    let _ = writeln!(out, "{phase}\tin_block\t0\t{}\t0", state.in_block as u8);
    let _ = writeln!(
        out,
        "{phase}\ttrap\t0\t{}\t0",
        state.trap.map(|trap| trap.0).unwrap_or(u32::MAX)
    );
    let _ = writeln!(out, "{phase}\tsteps\t0\t{}\t0", state.steps);
    let _ = writeln!(
        out,
        "{phase}\tat_loaded_entry\t0\t{}\t0",
        state.at_loaded_entry as u8
    );
    let _ = writeln!(out, "{phase}\tloop_depth\t0\t{}\t0", state.loop_depth);
    let _ = writeln!(
        out,
        "{phase}\tinstruction_clock\t0\t{}\t0",
        instruction_clock as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tinstruction_clock_base\t0\t{instruction_clock_base}\t0"
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_block_ok\t0\t{}\t0",
        state.cfg.block_ok as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_block_base_ok\t0\t{}\t0",
        state.cfg.block_base_ok as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_bank_model\t0\t{}\t0",
        state.cfg.bank_model as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_stack_model\t0\t{}\t0",
        state.cfg.stack_model as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_core_timer\t0\t{}\t0",
        state.cfg.core_timer as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tcfg_peripheral_model\t0\t{}\t0",
        state.cfg.peripheral_model as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tmode1\t0\t{}\t{}",
        state.r[MODE1].b, state.r[MODE1].m
    );
    write_pending(out, phase, state.pending);
    for (index, loop_state) in state.loops.items().iter().enumerate() {
        let _ = writeln!(
            out,
            "{phase}\tloop_start_sw\t{index}\t{}\t0",
            loop_state.start_sw
        );
        let _ = writeln!(
            out,
            "{phase}\tloop_end_sw\t{index}\t{}\t0",
            loop_state.end_sw
        );
        let _ = writeln!(
            out,
            "{phase}\tloop_remaining\t{index}\t{}\t0",
            loop_state.remaining
        );
        let _ = writeln!(out, "{phase}\tloop_mode\t{index}\t{}\t0", loop_state.mode);
    }
    for (index, value) in state.r.iter().enumerate() {
        let _ = writeln!(out, "{phase}\tureg\t{index}\t{}\t{}", value.b, value.m);
    }
}

fn write_pending(out: &mut String, phase: &str, pending: Option<Pending>) {
    let Some(pending) = pending else {
        let _ = writeln!(out, "{phase}\tpending_present\t0\t0\t0");
        return;
    };
    let _ = writeln!(out, "{phase}\tpending_present\t0\t1\t0");
    let _ = writeln!(
        out,
        "{phase}\tpending_target\t0\t{}\t0",
        pending.target.unwrap_or(-1)
    );
    let _ = writeln!(out, "{phase}\tpending_call\t0\t{}\t0", pending.call as u8);
    let _ = writeln!(out, "{phase}\tpending_slots\t0\t{}\t0", pending.slots);
    let _ = writeln!(
        out,
        "{phase}\tpending_return_from_call\t0\t{}\t0",
        pending.return_from_call as u8
    );
    let _ = writeln!(
        out,
        "{phase}\tpending_return_sw\t0\t{}\t0",
        pending.return_sw.unwrap_or(-1)
    );
}
