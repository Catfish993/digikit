//! ColdFire host-time and PC profile (feature `cf-profile`, off by default):
//! which execution path (RAM-clear batch, `run_fast` including fused loops,
//! `step_once`, the analytic idle skip) the instructions and the host time go
//! to, and a PC histogram (sampled every 64 instructions in `run_fast`, every
//! instruction in `step_once`, the idle skip weighted by instructions skipped).

use std::collections::HashMap;

use serde_json::{Value, json};

pub const PATHS: [&str; 4] = ["ram_clear", "run_fast", "step_once", "idle_skip"];

#[derive(Default)]
pub struct CfProf {
    pub ns: [u64; 4],
    pub insns: [u64; 4],
    pub calls: [u64; 4],
    /// PC -> weight in instructions.
    pub samples: HashMap<u32, u64>,
    pub idle_samples: HashMap<u32, u64>,
    /// Why `run_fast` returned: budget, pc range, observed pc, overflow,
    /// error, tick limit, exception/state, mmio, ipl.
    pub exits: [u64; 10],
    pub exit_pcs: HashMap<u32, u64>,
}

impl CfProf {
    pub fn add(&mut self, path: usize, ns: u64, insns: u64) {
        self.ns[path] += ns;
        self.insns[path] += insns;
        self.calls[path] += 1;
    }
    pub fn sample(&mut self, pc: u32, weight: u64) {
        *self.samples.entry(pc).or_default() += weight;
    }
    pub fn exit(&mut self, reason: usize, pc: u32) {
        self.exits[reason] += 1;
        if reason == 2 || reason == 7 {
            *self.exit_pcs.entry(pc).or_default() += 1;
        }
    }
    pub fn sample_idle(&mut self, pc: u32, weight: u64) {
        *self.idle_samples.entry(pc).or_default() += weight;
    }
    pub fn report(&self) -> Value {
        let top = |map: &HashMap<u32, u64>| {
            let mut rows: Vec<_> = map.iter().map(|(pc, w)| (*pc, *w)).collect();
            rows.sort_by_key(|row| std::cmp::Reverse(row.1));
            rows.truncate(400);
            rows.into_iter()
                .map(|(pc, w)| json!([format!("{pc:#x}"), w]))
                .collect::<Vec<_>>()
        };
        let paths: Vec<Value> = PATHS
            .iter()
            .enumerate()
            .map(|(i, name)| {
                json!({"path": name, "host_ns": self.ns[i], "instructions": self.insns[i],
                       "calls": self.calls[i]})
            })
            .collect();
        json!({"exits": self.exits, "exit_pcs": top(&self.exit_pcs), "paths": paths, "pc_samples": top(&self.samples),
               "idle_pc": top(&self.idle_samples)})
    }
}
