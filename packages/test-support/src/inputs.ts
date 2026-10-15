import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export type Device = "dt2" | "dn2";

// (device, version) -> base .syx and ready dir; same table as FIRMWARE in tools/patchkit/pk_harness.py.
// Override the ready dir with DIGI_PK_READY_<DEV>_<VER> (version without the dot).
const FIRMWARE: Record<string, { syx: string; ready: string }> = {
  "dt2 1.17": { syx: "Digitakt_II_OS1.17.syx", ready: "out/ready-pk-dt2-117" },
  "dn2 1.12": { syx: "Digitone_II_OS1.12.syx", ready: "out/ready-fix-dn2-112" },
  "dt2 1.16": { syx: "Digitakt_II_OS1.16.syx", ready: "out/ready-pk-dt2-116" },
  "dn2 1.11": { syx: "Digitone_II_OS1.11.syx", ready: "out/ready-pk-dn2-111" },
};

export interface ReadyInputs {
  syx: string;
  readyDir: string;
  cfSnapshot: string;
  dspImage: string;
  dspState: string;
}

export function repoRoot(): string {
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (;;) {
    if (fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) return dir;
    const up = path.dirname(dir);
    if (up === dir) throw new Error("repo root (pnpm-workspace.yaml) not found");
    dir = up;
  }
}

/** The .syx and ready-input files of a device and firmware version; throws when one is missing. */
export function resolveInputs(device: Device, firmware: string): ReadyInputs {
  const root = repoRoot();
  const spec = FIRMWARE[`${device} ${firmware}`];
  let syx: string;
  let readyDir: string;
  if (spec) {
    syx = path.join(root, "firmware", spec.syx);
    const env = process.env[`DIGI_PK_READY_${device.toUpperCase()}_${firmware.replace(".", "")}`];
    readyDir = env ?? path.join(root, spec.ready);
  } else {
    throw new Error(`no firmware entry for ${device} ${firmware}`);
  }
  const ready = {
    syx,
    readyDir,
    cfSnapshot: path.join(readyDir, "digi-audio-m5.snap"),
    dspImage: path.join(readyDir, "digi-audio-dn2-image.bin"),
    dspState: path.join(readyDir, "digi-audio-m5.snap.dsp"),
  };
  for (const f of [ready.syx, ready.cfSnapshot, ready.dspImage, ready.dspState]) {
    if (!fs.existsSync(f)) {
      throw new Error(
        `${f} is missing; build the ready dir with ` +
          `\`uv run python -m tools.emulator.ready ${device} --syx ${path.basename(syx)} --out ${readyDir} --force\``,
      );
    }
  }
  return ready;
}
