//! Scripted inputs for coupled runs (scenarios.json), shared by coupled-run and the desktop app's --scenario mode.
use serde_json::Value;

/// DSPI2 frames per audio second (one 32-sample block each).
pub const FRAMES_PER_SECOND: f64 = 1500.0;
pub const DEFAULT_SCENARIOS: &str = include_str!("../scenarios.json");

#[derive(Clone, Copy, Debug)]
pub enum When {
    Instr(u64),
    Frames(u64),
}

#[derive(Clone, Copy, Debug)]
pub enum Action {
    Button { code: u8, down: bool },
    Turn { encoder: u8, detents: i32 },
}

#[derive(Clone, Copy, Debug)]
pub struct Event {
    pub when: When,
    pub action: Action,
}

#[derive(Debug)]
pub struct Scenario {
    pub name: String,
    pub description: String,
    /// End after this many audio seconds, or this many ColdFire instructions.
    pub seconds: Option<f64>,
    pub instructions: Option<u64>,
    pub qa_script: bool,
    /// With `instructions`: shorten the last chunk so the run ends exactly there
    /// (the desktop Trig 1 test does); otherwise chunks are always full, as in the app.
    pub clip_last_chunk: bool,
    /// Shorten chunks so each `at_instr` event fires at exactly that count.
    pub exact_events: bool,
    /// SHA-256 the run's whole PCM must have (a recorded reference).
    pub expect_sha256: Option<String>,
    pub events: Vec<Event>,
}

pub fn parse_scenarios(text: &str) -> Result<Vec<Scenario>, String> {
    let root: Value = serde_json::from_str(text).map_err(|e| format!("scenario file: {e}"))?;
    let list = root["scenarios"]
        .as_array()
        .ok_or("scenario file needs a \"scenarios\" array")?;
    list.iter().map(parse_scenario).collect()
}

pub fn parse_scenario(v: &Value) -> Result<Scenario, String> {
    let name = v["name"]
        .as_str()
        .ok_or("scenario without a name")?
        .to_string();
    let err = |m: &str| format!("scenario {name}: {m}");
    let seconds = v["seconds"].as_f64();
    let instructions = v["instructions"].as_u64();
    if seconds.is_none() == instructions.is_none() {
        return Err(err("give exactly one of \"seconds\" and \"instructions\""));
    }
    let mut events = Vec::new();
    for e in v["events"].as_array().map_or(&[][..], Vec::as_slice) {
        let when = match (e["at_s"].as_f64(), e["at_instr"].as_u64()) {
            (Some(s), None) if s >= 0.0 => When::Frames((s * FRAMES_PER_SECOND).round() as u64),
            (None, Some(n)) => When::Instr(n),
            _ => return Err(err("an event needs one of at_s (>= 0) and at_instr")),
        };
        let action = if let Some(code) = e["button"].as_u64() {
            Action::Button {
                code: u8::try_from(code).map_err(|_| err("button code over 255"))?,
                down: e["down"].as_bool().unwrap_or(true),
            }
        } else if let Some(enc) = e["turn"].as_u64() {
            Action::Turn {
                encoder: u8::try_from(enc).map_err(|_| err("encoder over 255"))?,
                detents: i32::try_from(e["detents"].as_i64().unwrap_or(1))
                    .map_err(|_| err("detents out of range"))?,
            }
        } else {
            return Err(err("an event needs \"button\" or \"turn\""));
        };
        events.push(Event { when, action });
    }
    Ok(Scenario {
        description: v["description"].as_str().unwrap_or("").to_string(),
        name,
        seconds,
        instructions,
        qa_script: v["qa_script"].as_bool().unwrap_or(false),
        clip_last_chunk: v["clip_last_chunk"].as_bool().unwrap_or(false),
        exact_events: v["exact_events"].as_bool().unwrap_or(false),
        expect_sha256: v["expect_sha256"].as_str().map(str::to_string),
        events,
    })
}

pub fn fire(rt: &mut crate::CoupledRuntime, action: Action) -> Result<(), String> {
    match action {
        Action::Button { code, down } => rt.button(code, down),
        Action::Turn { encoder, detents } => rt.turn(encoder, detents),
    }
}

/// The scenario inside `value`: `value` itself when it has an "events" array,
/// else the first "recorded_scenario" found in its objects (a desktop
/// diagnostics export carries one).
pub fn find_scenario(value: &serde_json::Value) -> Option<&serde_json::Value> {
    if value.get("events").is_some_and(serde_json::Value::is_array) {
        return Some(value);
    }
    let object = value.as_object()?;
    if let Some(found) = object.get("recorded_scenario").and_then(find_scenario) {
        return Some(found);
    }
    object.values().find_map(find_scenario)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn find_scenario_reads_a_bare_scenario_and_a_nested_recording() {
        let bare = json!({"events": []});
        assert_eq!(find_scenario(&bare), Some(&bare));
        let nested = json!({"diagnostics": {"recorded_scenario": {"name": "r", "instructions": 5, "events": []}}});
        let found = find_scenario(&nested).unwrap();
        assert_eq!(found["name"], "r");
    }

    #[test]
    fn parse_scenario_reads_exact_events() {
        let sc = parse_scenario(&json!({"name":"x","instructions":10,"exact_events":true,"events":[{"at_instr":4,"button":25,"down":true}]})).unwrap();
        assert!(sc.exact_events);
        assert_eq!(sc.events.len(), 1);
    }
}
