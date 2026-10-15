// Panel key and encoder names, ported from tools/patchkit/pk_harness.py (BUTTONS, ENCODERS, _norm).

export const BUTTONS: Record<string, number> = {
  "PAGE TRIG": 1,
  SRC: 2,
  SYN: 2,
  FLTR: 3,
  AMP: 4,
  FX: 5,
  MOD: 6,
  PRESET: 7,
  SETTINGS: 8,
  TEMPO: 9,
  YES: 10,
  UP: 11,
  NO: 12,
  LEFT: 13,
  DOWN: 14,
  RIGHT: 15,
  TRK: 16,
  FUNC: 17,
  KEYBOARD: 18,
  RECORD: 19,
  PLAY: 20,
  STOP: 21,
  PAGE: 22,
  PTN: 23,
  SONG: 24,
  "LEVEL PUSH": 49,
  ...Object.fromEntries(Array.from({ length: 16 }, (_, i) => [`TRIG ${i + 1}`, 25 + i])),
  ...Object.fromEntries([..."ABCDEFGH"].map((c, i) => [`PUSH ${c}`, 41 + i])),
};

export const ENCODERS: Record<string, number> = {
  ...Object.fromEntries([..."ABCDEFGH"].map((c, i) => [`ENC ${c}`, 1 + i])),
  LEVEL: 9,
};

function norm(name: string): string {
  return name.trim().toUpperCase().replace(/[\s_]+/g, " ");
}

export function button(name: string | number): number {
  if (typeof name === "number") return name;
  const code = BUTTONS[norm(name)];
  if (code === undefined) throw new Error(`unknown button ${JSON.stringify(name)}`);
  return code;
}

export function encoder(name: string | number): number {
  if (typeof name === "number") return name;
  const code = ENCODERS[norm(name)];
  if (code === undefined) throw new Error(`unknown encoder ${JSON.stringify(name)}`);
  return code;
}
