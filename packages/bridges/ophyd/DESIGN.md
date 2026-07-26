# labwire-ophyd: mapping model and rationale

Design notes for the bridge. The user-facing quickstart lives in the package
README; this document records *why* the mapping is what it is, and what it
cannot do. Every ophyd behavior cited here was verified against **ophyd
1.11.2 on Python 3.12**, not recalled.

## Position

ophyd abstracts hardware for Python programmers. Labwire describes hardware
to AI agents. The bridge composes them: Labwire gets access to the largest
existing body of Python instrument drivers without reimplementing any of it,
and ophyd devices become discoverable, unit-typed, safety-classified, and
signable. ophyd is an **optional dependency**, never vendored or modified
(BSD-3; see the repository `NOTICE`).

## Mapping table

| ophyd | Labwire |
|---|---|
| `Device.name` | `IdentityInfo.serial_number` (model = class name, manufacturer = "ophyd bridge (Labwire)", firmware = the ophyd version) |
| `Kind.hinted` (5) / `Kind.normal` (1) | telemetry channel, plus `set_<attr>` if settable |
| `Kind.config` (2) | descriptor metadata only — never a settable command |
| `Kind.omitted` (0) | skipped entirely |
| `describe()[key]["units"]` | UCUM unit, after EGU translation (§ Units) |
| `lower_ctrl_limit` / `upper_ctrl_limit` | JSON Schema `minimum` / `maximum` on the set parameter |
| `describe()[key]["dtype"]` | channel dtype: `number`→`float64`, `integer`→`int64`, `boolean`→`bool`, `string`→`string` |
| `Device.set()` | `set_<attr>` command, safety class **S2** |
| `Device.trigger()` | `trigger` command, safety class **S1** |
| `Device.stop()` | `stop` command, safety class **S0** |
| `MoveStatus.watch()` | `ctx.progress(...)` |
| `MoveStatus` failure / `exception` | `HardwareFaultError`; timeout → `DeviceTimeoutError` |
| Labwire `command/cancel` | `device.stop(success=False)` → `CanceledError` |

### Naming

ophyd flattens data keys as `{device.name}_{attr}`, except that a
positioner's primary readback takes the bare device name — `SynAxis(name="ax")`
produces the key `ax`, not `ax_readback`. The bridge uses ophyd's own keys as
channel names so that data recorded through Labwire and through Bluesky line
up. Note that ophyd also reserves some attribute names (`position` among
them) for the bluesky interface and refuses them as component names.

## Units (the first hard problem)

Labwire v0.2 requires a UCUM code on every quantity. ophyd's `describe()`
surfaces `units` **only for EPICS-backed signals** — `EpicsSignalBase.describe()`
adds `units`, `lower_ctrl_limit`, `upper_ctrl_limit`, `precision`, and
`enum_strs` — and even then the value is a free-text EGU string that carries
no guarantee of being valid UCUM.

Resolution, in order, with no silent defaults:

1. **Adopt** the unit from `describe()` when present.
2. **Translate** it through an explicit EGU→UCUM table (`_egu.py`) covering
   conventional beamline spellings (`microns`→`um`, `degC`→`Cel`,
   `counts`→`{counts}`, …). Unknown strings are *unresolved*, never passed
   through.
3. **Annotate or refuse.** Anything still unresolved is reported by exact
   component name; the instrument is refused unless `--allow-partial` omits
   the offending signals, each of them reported.

A blank EGU — the commonest EPICS case — is treated as *absent*, never as
`"1"`. Dimensionless is only ever asserted by a human in the annotation file.

**Honest limitation:** `ophyd.sim` devices carry no `units` key at all
(verified on `SynAxis` and `SynGauss`), so the auto-adopt path cannot be
exercised by any simulated device. Its tests use a signal double that mirrors
`EpicsSignalBase.describe()` exactly. Only a real Channel Access layer
(milestone B5's caproto soft IOC) would prove it end to end.

## Safety classes (the second hard problem)

ophyd carries no safety semantics whatsoever. The bridge therefore cannot
infer them, and guessing low is the failure mode that moves hardware, so the
defaults lean toward friction:

| Command | Default | Why |
|---|---|---|
| `set_<attr>` | **S2** | Actuation: it moves or changes the device, and may be irreversible |
| `trigger` | **S1** | Acquisition is reversible and consumes nothing |
| `read` | **S1** | A pure read |
| `stop` | **S0** | The recovery path; must stay submittable while interlocked |

Raising a class (a shutter, a destructive measurement) or lowering one (a
demonstrably harmless axis) requires an **explicit** annotation entry. There
is no heuristic that infers safety from a device's shape, because a wrong
inference here has physical consequences.

## Known limitations

- **Simulated devices only.** No claim is made about real EPICS hardware, any
  beamline, or NSLS-II. Nothing enters the README that CI cannot demonstrate.
- **Classic synchronous ophyd only.** `ophyd-async` is future work, not
  attempted here.
- ophyd is blocking, so every ophyd call runs in `asyncio.to_thread`; a
  badly behaved device still occupies a worker thread.
- `describe()` infers dtype from the current value for sim signals (an axis
  resting at integer `0` reports `integer`), so dtype is a hint an annotation
  can override.
- Array- and enum-valued signals are refused: Labwire v0.2 channels carry
  scalars.
- The EGU→UCUM table is convention-based and not validated against a UCUM
  implementation (`TODO-VERIFY`).
