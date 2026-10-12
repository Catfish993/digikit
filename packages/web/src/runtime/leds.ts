// Front-panel LED values from the firmware's own panel records (native/boot panel_leds.rs).
// The value semantics are provisional: high nibble = brightness 0-7, low nibble = colour/mode, 0x01 = unlit.
// Everything that turns a value into light lives in `ledLight`, so a corrected mapping is a one-function change.

export const LED_COUNT = 64;

export function parseLeds(hex: string): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(LED_COUNT);
  for (let i = 0; i < LED_COUNT && i * 2 + 1 < hex.length; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export interface Light { on: boolean; intensity: number }

/** Intensity 0..1 for one LED value. */
export function ledLight(value: number): Light {
  const brightness = value >> 4;
  if (brightness === 0) return { on: false, intensity: 0 };
  return { on: true, intensity: 0.3 + 0.7 * (brightness - 1) / 6 };
}

export const WARM_WHITE = '#ffe3a6';
/** Default colour for a key's lit legend: PLAY green, RECORD red, the rest warm white. */
export const defaultLedColor = (icon?: string) => icon === 'play' ? '#46d160' : icon === 'record' ? '#ff5a4f' : WARM_WHITE;
