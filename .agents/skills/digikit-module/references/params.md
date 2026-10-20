# Parameters and knobs: from "the user wants these knobs" to `module.toml`

Targets: Digitakt II 1.17 (dt2) and Digitone II 1.12 (dn2). Facts marked [D] were measured once, [O] are open, "not known" means no source
states it. `manifest.md` has the key list; this file has the rules behind the keys.

## 1. What a parameter is

A parameter is one 16-bit word that the ColdFire keeps in the sound array and sends to the DSP every block. The module declares its range, its
default and its labels. The UI shows the word as 0 to 127 over `min` .. `max` ([D], `docs/findings/02-machines-and-parameters.md:1217`).

`sdk/dk_machine.h:17-22` names four kinds. They are conventions for the word, not a manifest key:

| kind | word range | UI | rest value | use for |
| --- | --- | --- | --- | --- |
| `DK_P_PERCENT` | 0 .. 0x7f00 | 0 .. 127 | none, use `default` | amounts, times, mix, decay |
| `DK_P_BIPOLAR` | 0 .. 0x7f00 | -64 .. +63 | 0x4000 | detune, pan, tilt |
| `DK_P_ENUM` | step x 256 | step number | step 0 | a switch or a mode with n steps |
| `DK_P_PITCH` | 0 .. 0x7f00 | semitone offset | 0x4000 | a pitch offset the adapter adds to `pitch` |

- `[[param]]` has no `kind` key. `ParamDef` (`packages/patchkit/src/typed.rs:175-189`) lists id, name, short, template, min, max, default,
  devices. An extra key such as `kind = "bipolar"` is refused as an unknown device table (`typed.rs:364-377`).
- The kind is set by `min`, `max`, `default` and your DSP code. The shown value and label come from the `template` row. Only `0xd1` (dt2) and
  `0xd5` (dn2) are used as templates for a plain 0 .. 127 value (`pluck/module.toml:57,63`). A template with a signed or named display:
  not known.
- The enum rule in the table (word = step x 256, `max` = (n-1) x 256) follows `dk_machine.h:20`. No module uses it yet: untested.
- The DSP converts a word to float with a scale of 1/32512 (0x7f00 = 1.0) in the stock code (`docs/findings/53-stock-dsp-shape.md:51-52`). Your
  routine may use any scale; pluck documents its own in `modules/machines/pluck/src/dsp/pluck.s`.

## 2. Ranges, defaults and the clamp

- `min`, `max`, `default` are words, 0 to 0x7f00. Stock stores raw s16 and does not clamp (`53-stock-dsp-shape.md:159-160`). Write them as
  `0x7f00` for 127, `0x4000` for 64.
- The adapter clamps each parameter word to its `[min, max]` before `render` and `note_on` see it, in its own buffer
  (`modules/core/dsp-voice/src/dsp/dsp-voice.s:1307-1329`; `sdk/dk_machine.h:50,53`). The reason is an LFO or a lock that drives a value
  out of range (`docs/findings/51-pluck-tune-guard.md:5-12`).
- A range outside 0 .. 0xffff or `min` above `max` in the clamp table is treated as undeclared: the clamp is then 0 .. 0x7f00
  (`typed.rs:651-670`). The pack step refuses `min` above `max` (`packages/patchkit/src/register.rs:260-262`).
- `default` is the value a new sound gets when the machine is selected ([D], `02-machines-and-parameters.md:1218`).
- Not found in `typed.rs`: a check that `default` lies in `min` .. `max`. `docs/design/module-sdk.md:463` plans one. Keep it true yourself.
- Names: `name` at most 31 characters, `short` at most 15, printable ASCII (`register.rs:44-45,137-143,258-259`).
  `[machine] name` and `short` at most 23 (`register.rs:43,266-267`).

## 3. Knobs per device

`knobs[j]` is knob j, 0 to 7. The position is the slot. A number is a stock row, `0` is empty, a string is a `[[param]]` id
(`docs/MODULE-AUTHORING.md:110-112`).

### Digitakt II (machine row, 8 knobs, engine slots 0 to 7)

- Knobs are the fields `f0..f7` of the machine row, the level is `f8` (`devices/digitakt-ii.toml:294-301`). Slot s has mirror index
  25 + s + (s / 4) x 2, so 25, 26, 27, 28, 31, 32, 33, 34 (`devices/digitakt-ii.toml:312-319`; `02-machines-and-parameters.md:624-632`).
- Mirror indexes are positional: a machine chooses what each slot means, not where it sits (`02-machines-and-parameters.md:618-622`).
- A SAMPLE clone (`clone_of = 0`) has: knob 0 TUNE (`0xca`), 1 PLAY, 2 empty in stock, 3 SAMP, 4 STRT, 5 LEN, 6 LOOP, 7 LEV (`0xd1`)
  (`02-machines-and-parameters.md:638-647`). Any slot may be replaced by a module parameter except knob 3.
- Knob 3 is refused: it is the sample word. The TX word is `value & 0x3ff`, and the module default replaces the track's sample number
  ([D], `02-machines-and-parameters.md:1653-1667`; `devices/digitakt-ii.toml:303-310`). Knob 6 is usable ([D], same place).
- Knob 7 is a normal slot. The track level is the separate `level` (code 0x0a), outside the knobs (`53-stock-dsp-shape.md:159-160`). Pluck
  keeps `0xd1` (LEV) on knob 7 by choice (`pluck/module.toml:42`). `tests/fixtures/modules/dyn-params/module.toml:27` puts a parameter there.
- A page shows 8 of the 10 engine slots (25 to 34). A second SRC page is not possible, and an added machine has one SRC page
  ([D], `docs/findings/50-dt2-src-second-page.md`, section "A second DT2 page is not possible yet"). `[[page]]` for dt2 is a build error:
  "a second SRC page for a Digitakt II machine is not supported yet" (`typed.rs:537-540`).

### What reaches the DSP (read this before choosing knobs)

The contract passes the DSP words, not knob numbers. `params[i]` is the word of knob `param_slots[i]`
(`modules/core/dsp-voice/src/dsp/dsp-voice.s:1209-1230`; `packages/patchkit/src/devgen.rs:464-474`):

| device | `param_slots` | `params[0..3]` are knobs | `reserved_slots` |
| --- | --- | --- | --- |
| dt2 | `[1, 2, 4, 5]` | 1, 2, 4, 5 | `[3]` |
| dn2 | `[0, 1, 2, 3]` | 0, 1, 2, 3 | none |

(`devices/digitakt-ii.toml:335-336`, `devices/digitone-ii.toml:315-316`.) Words beyond the list stay 0 (`dsp-voice.s:1209`). The TUNE knob is
folded into `pitch` (`devices/digitakt-ii.toml:334`).

- So a contract machine can read four parameters on each device. A parameter on another knob (dt2 knob 0, 6, 7) is stored, shown and saved,
  but no `params` word carries it. The build does not refuse this: no check was found. `module-sdk.md:290` plans one. Treat 4 as the limit.
- Order the parameters by knob position, so that `params[i]` is the i-th parameter in the knob list. Pluck does: decay, pick, bright,
  color are `params[0..3]` (`pluck/module.toml:42`).

### Digitone II (page, 8 knobs, sound-array slots 25 to 32)

- The machine has no knob layout in its row. It takes `[machine.dn2] knobs = [...]` and a `[[page]]` (`MODULE-AUTHORING.md:107`).
  Slot = 25 + knob position (`devices/digitone-ii.toml:284-292`).
- A stock page descriptor is two names and nine codes: eight knobs and the track level, code 10 (`02-machines-and-parameters.md:1327`).
  Stock pages: FM TONE 4, WAVETONE 3, FM DRUM 4, SWARMER 1, MIDI 1.
- A machine with a page of its own sets `own_dsp = true`; the FM engine then renders nothing for it (`devices/digitone-ii.toml:285`).
- A stock code on a page is refused (`typed.rs:549`); use `0` or a parameter name. Pluck has no TUNE and no LEV on dn2 (`02:1550`).
- The typed `[[page]]` gives one page per machine. Its slots are 25 + position, so a second typed page would reuse slots 25 to 32: not
  known whether the build refuses it. A two-page module uses hand-written `registry = "pages"` rows with `s0..` slots, as
  `tests/fixtures/modules/dn2-pages/module.toml:105-130` (slots 40 and 41; patched-in, `02-machines-and-parameters.md:1479`).
- Rows per module: at most 8 pages (`register.rs:42`).

### Build errors for `knobs` (`packages/patchkit/src/typed.rs`)

| rule | message (start) | line |
| --- | --- | --- |
| more knobs than the device has | `{module}: machine {id}: 9 knobs, but dt2 has 8` | 422 |
| parameter on a reserved knob | `{module}: machine {id}: param "x" sits at knob 3, but knob 3 of a SAMPLE clone is the sample slot: only the sample number bits reach the DSP (docs/findings/02-machines-and-parameters.md)` | 455 |
| name is no `[[param]]` | `{module}: {owner}: knob 1 names "x", which is not a [[param]] of this module` | 484 |
| `[[param]]` limited to another device | `... knob j names "x", which is not a param of dn2` | 489 |
| a parameter twice in one list | `... knob 2 names "x", which knob 1 already names; a param appears once in knobs` | 494 |
| one parameter at two positions (two machines, or machine and page) | `{module}: param x sits at knob 1 and knob 2 ({owner}); one param has one slot` | 501 |
| `[[param]]` that no machine of the device uses | `{module}: param x: no machine of dn2 uses it` | 512 |
| `own_dsp` on a device without it | `{module}: machine {id}: dt2 has no own_dsp machines` | 429 |
| stock code on a page | `{module}: page {id}: knob j: a stock code on a page is not supported yet (name a param)` | 549 |
| `type_hint` outside 0 .. 255 | `... type_hint N is not a type number` | 434 |

A `[[param]]` with `devices` unset that only one device's machine uses is left out on the other device without an error
(`MODULE-AUTHORING`/`modules/README.md:380`). With explicit `devices` it is an error (line 512).

## 4. What persists, what can be locked or modulated

- **Persistence, dt2: works [D].** The engine slots are the stock mirror indexes 25 to 34, saved by the stock permutation tables; no storage
  change. A new row's slot must be one of the eight slots (`02-machines-and-parameters.md:1219`). Values are raw s16.
- **Persistence, dn2: works [D].** Pluck's four values come back after save and load (`02:1547`).
- Projects store values by slot. Do not move a released parameter to another knob; `module:export --previous` checks it
  (`MODULE-AUTHORING.md:118-119`).
- If the module is absent at load, the track keeps its type and saves it back unchanged ([D], `docs/findings/39-digikit-boot-tests.md:28-35`).
  A stock firmware loads the track as SAMPLE (dt2) or FM TONE (dn2) with the values in the clone's slots, and loses the type on re-save
  ([D], `02:1219`, `1547`; the re-save loses the type: `02:970-981`, dt2 only).
- **p-lock: not shown [O].** Locks are stored by mirror index, so a parameter in an existing slot should lock like a stock one
  (`02:1221`). A p-lock on an added machine went through a save and load round trip in the boot test (`39:28`); the lock value itself was not decoded (`39:41`).
  On dn2, not tried (`02:1525`).
- **LFO and modulation destination:** the MOD page lists a dt2 module's parameter names in the SRC group ([D], `02:1220`). That an LFO
  moves a module parameter on the DSP: not known (only the stock TUNE was observed, `51-pluck-tune-guard.md:9`). dn2: not known. Because an
  LFO can drive a word out of range, the clamp in section 2 stays on.
- **MIDI CC and NRPN: ignored** for added rows on both devices ([D], `02:1222`, `1502`; `53-stock-dsp-shape.md:158`).
- FX parameters are a different system: not lockable, not LFO destinations, and a module cannot add them with `[[param]]`
  (`53-stock-dsp-shape.md:157-169`).

## 5. A worked mapping: pluck

Request: "a plucked string with four knobs: decay, pick position, brightness, colour; keep TUNE and LEVEL."

1. One `[[param]]` per knob, with stable ids, a template, a range and a default (`pluck/module.toml:53-60`):
   ```
   [[param]]
   id = "pluck_decay"   name = "Decay"   short = "DECAY"
   template = 0xd1   min = 0   max = 0x7f00   default = 0x4000
   [param.dn2]
   template = 0xd5
   ```
   (the file writes each key on its own line). Defaults: decay 0x4000, pick 0x2000, bright 0x5000, color 0x2800 (lines 60, 72, 84, 96).
2. Pick dt2 knob positions. Slots 1, 2, 4, 5 are the four that reach the DSP; 3 is refused; 0 and 7 keep TUNE and LEV (line 42):
   `knobs = [0xca, "pluck_decay", "pluck_pick", 0, "pluck_bright", "pluck_color", 0, 0xd1]`
   Knob 3 and knob 6 are `0`.
3. dn2 has no TUNE or LEV row and no reserved knob. Use knobs 0 to 3 and a page (lines 44-51, 101-106):
   ```
   [machine.dn2]
   own_dsp = true
   knobs = ["pluck_decay", "pluck_pick", "pluck_bright", "pluck_color"]
   [[page]]
   id = "pluck_syn"  machine = "pluck"  name = "PLUCK"  title = "Pluck"  devices = ["dn2"]
   ```
4. The DSP reads `params[0..3]` as decay, pick, bright, color on both devices. The voice row names `render`, `note_on` and `desc`;
   `desc` holds `n_params = 4` (`sdk/dk_machine.h:32-37`, `references/contract-voice.md`).
5. For "N knobs" in general: N at most 4 contract parameters. Put them on dt2 knobs 1, 2, 4, 5 in the order the DSP reads them, and on dn2
   knobs 0 to 3. A fifth control on dt2 can only be a stock row (`0xca`, `0xd1`) or a stored-only parameter (not in `params`).
6. Run `mise run module:check -- AUTHOR/NAME --device dt2` and `--device dn2`. The conformance suite drives every parameter to its limits.

## 6. Limits

| limit | value | source |
| --- | --- | --- |
| knobs per machine | 8 (dt2 and dn2) | `devices/digitakt-ii.toml:296`, `devices/digitone-ii.toml:287` |
| parameter words the DSP receives | `DK_MAX_PARAMS` = 8 in the contract; `param_slots` has 4 on each device, so 4 are filled | `sdk/dk_machine.h:7`, `sdk/sdk.toml:22`, `devices/*.toml` |
| `[[param]]` per module (run-time pack) | 16 | `register.rs:40,248-255` |
| machines per module | 8 | `register.rs:41` |
| pages per module | 8 | `register.rs:42` |
| records in a register section | 32 | `register.rs:35` |
| `param_slots` entries | at most 8, each 0 to 7, none also in `reserved_slots` | `devgen.rs:338-356` |

- `DK_MAX_PARAMS` is "a hard cap; the device profile gives the real maximum" (`sdk/dk_machine.h:7`). The profile value is `param_slots`: 4.
- The build refuses what section 3 lists, plus the length and range rules in section 2. It does not refuse a parameter outside
  `param_slots` or a `default` outside `min` .. `max` (not found).
- A fixed machine type number is never written for a run-time machine; leave `type_hint` out unless you need the old number
  (`SKILL.md`, rules; `typed.rs:433`).
- Changing `devices/*.toml` needs `mise run gen:modules`.
