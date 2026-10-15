// Param and result shapes of the control protocol (docs/CONTROL.md). Results list the fields tests use;
// the daemon may send more.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export const ERR = {
  noFirmware: 1,
  staleSession: 2,
  busy: 3,
  rejected: 4,
  audio: 5,
  fault: 6,
  dspHalted: 7,
  io: 8,
  timeout: 9,
} as const;

export type EventKind =
  | "halt"
  | "stall.end"
  | "underrun"
  | "session.changed"
  | "scenario.done"
  | "firmware.loaded"
  | "mode"
  | "card.started"
  | "card.ready"
  | "card.error"
  | "lagged"
  | "frame"
  | (string & {});

/** A pushed event; `kind` plus kind-specific fields. */
export interface EmuEvent {
  kind: EventKind;
  [key: string]: unknown;
}

export type StartMode = "auto" | "snapshot" | "cold";

export interface FirmwareLoadParams {
  syx: string;
  base_sha256?: string;
  start?: StartMode;
  card?: string;
  dsp_patch?: string;
  force?: boolean;
  auto_no?: boolean;
}

export interface FirmwareLoadResult {
  start_used?: string;
  main_changed?: boolean;
  sections_changed?: unknown;
  cache_hit?: boolean;
  seconds?: number;
  [key: string]: unknown;
}

export interface StepParams {
  instructions: number;
  timeout_wall_s?: number;
  [key: string]: unknown;
}

export interface RunUntilParams {
  frames?: number;
  audio_s?: number;
  elapsed?: number;
  icount?: number;
  timeout_wall_s?: number;
  [key: string]: unknown;
}

export interface InputScheduleEvent {
  at_instr?: number;
  [key: string]: unknown;
}

export interface FramebufferParams {
  hash?: boolean;
  data?: boolean;
}

export interface FramebufferResult {
  revision: number;
  /** base64 of the 1024 decoded OLED bytes; absent when `data: false`. */
  data_b64?: string;
  /** hex SHA-256 of the 1024 decoded OLED bytes; present when `hash: true`. */
  sha256?: string;
}

export interface LedsResult {
  led_revision: number;
  /** 128 hex digits (64 LED values). */
  leds: string;
  /** 384 hex digits (64 x r, g, b) or null until any is set. */
  rgb: string | null;
}

export interface CfStatus {
  phase: string;
  ready: boolean;
  main_ui_reached: boolean;
  pc: number;
  error?: string | null;
  filesystem_verified?: boolean | null;
  [key: string]: unknown;
}

export interface Heartbeat {
  stalled: boolean;
  stall_count: number;
  stall_ms: number;
  mainloop_hits: number;
  [key: string]: unknown;
}

export interface StatusResult {
  cf: CfStatus;
  heartbeat: Heartbeat;
  /** "zero", "sharc" or "none". */
  dsp?: "zero" | "sharc" | "none" | (string & {});
  audio?: { halted?: unknown; [key: string]: unknown } | null;
  [key: string]: unknown;
}

export interface WatchdogResult {
  [key: string]: unknown;
}

export interface GuardRange {
  lo: number;
  hi: number;
}
export interface GuardProtect extends GuardRange {
  label: string;
}
export interface GuardSetParams {
  protect: GuardProtect[];
  allow_pc: GuardRange[];
}
export interface GuardHit {
  pc: number;
  addr: number;
  size: number;
  label: string;
  icount: number;
}
export interface GuardReport {
  total: number;
  hits: GuardHit[];
}
