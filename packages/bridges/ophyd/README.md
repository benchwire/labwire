# labwire-ophyd

**Expose any [ophyd](https://github.com/bluesky/ophyd) device as a Labwire
instrument**, so AI agents can discover and drive the largest existing body
of Python instrument drivers through one protocol, with the units, safety
classes, and signed results ophyd itself does not carry.

ophyd abstracts hardware *for Python programmers*. Labwire describes hardware
*to AI agents*. This bridge composes them; Labwire does not reimplement
drivers.

## Five-minute quickstart

Nothing here needs hardware. From a checkout with `make setup` done:

```bash
# 1. See what the bridge works out on its own, and what it cannot
uv run labwire-ophyd annotate ophyd.sim:motor -o labwire-ophyd.yaml

# 2. Fill in every TODO in that file (ophyd.sim reports no units at all),
#    then check what Labwire would expose
uv run labwire-ophyd check ophyd.sim:motor --annotations labwire-ophyd.yaml

# 3. Watch a full scan run through the protocol, with signed evidence
make demo-ophyd
```

`labwire-ophyd check` prints the resolved instrument: every channel with its
UCUM unit, every command with its safety class:

```
OK: SynAxis (motor)
  2 channel(s), 4 component(s), 3 command(s)
    S1  read
    S2  move
    S0  stop
    motor: mm
    motor_setpoint: mm
```

Serving it is a few lines:

```python
from labwire.bridges.ophyd import OphydInstrument, load_annotations
from labwire.core import InstrumentServer

instrument = OphydInstrument(device, load_annotations(Path("labwire-ophyd.yaml")))
server = InstrumentServer(instrument, confirmation_token="operator-grant")
async with server.serve_websocket("127.0.0.1", 9520):
    ...
```

## The annotation file

ophyd knows a device's structure but not its meaning: `ophyd.sim` devices
report no units, EPICS reports free-text `.EGU` strings, and ophyd has no
concept of a safety class at all. The annotation file supplies exactly that
layer, and it is what makes a device safe for an agent to operate.

```yaml
version: 1
devices:
  ophyd.sim.SynAxis:                 # keyed by class; subclasses inherit it
    description: A sample stage.
    intent_tags: [motion]
    components:
      readback:
        unit: mm                     # UCUM, never guessed for you
        qudt_quantity_kind: Length
        dtype: float64               # ophyd infers dtype from the current value
      setpoint: {unit: mm, dtype: float64}
      velocity: {unit: mm/s}
      acceleration: {unit: mm/s2}
    commands:
      trigger: {exclude: true}       # meaningless for a stage
      move: {estimated_duration_s: 2.0}

instances:
  fine_stage:                        # keyed by Device.name; overrides the class
    components:
      setpoint: {limits: {low: -5.0, high: 5.0}}
```

Rules worth knowing:

- **Merging is per field**, along the class MRO and then the instance, so an
  override touches only what it names.
- **Unknown keys, components, and commands are errors.** A typo that
  silently annotated nothing would be worse than a failure.
- **Missing units are refused**, naming the exact component and the YAML path
  that would fix it. `--allow-partial` omits those components instead, and
  still reports every one.
- Annotated `limits` **intersect** with the device's own control limits: the
  tightest bound on each side wins.
- `settable: false` keeps a channel but removes its actuation command, for
  the readings ophyd technically lets you write, such as a detector's computed
  value.

A working example lives at
[`examples/ophyd_scan/labwire-ophyd.yaml`](../../../examples/ophyd_scan/labwire-ophyd.yaml).

## Mapping

| ophyd | Labwire |
|---|---|
| `Device.name` | instrument serial number (model = class name) |
| `Kind.hinted` / `Kind.normal` | telemetry channel |
| `Kind.config` | descriptor metadata |
| `Kind.omitted` | skipped |
| `describe()[…]["units"]` | UCUM unit, after EGU translation |
| `lower_ctrl_limit` / `upper_ctrl_limit` | parameter `minimum` / `maximum` |
| `Device.set()` on a positioner | `move`, safety class **S2** |
| `signal.set()` elsewhere | `set_<attr>`, safety class **S2** |
| `Device.trigger()` | `trigger`, safety class **S1** |
| `Device.stop()` | `stop`, safety class **S0** |
| `command/cancel` | `device.stop()`, and the run ends canceled |
| ophyd failures | `HardwareFaultError` / `DeviceTimeoutError` |

Safety defaults lean toward friction, because ophyd carries no safety
information and guessing low is the failure that moves hardware: actuation is
S2 (an agent must present an operator confirmation), acquisition is S1, and
`stop` is S0 so recovery stays available even while an interlock is tripped.
Changing a class takes an explicit annotation, never a heuristic.

[DESIGN.md](DESIGN.md) has the full reasoning, including why positioners are
moved rather than poked.

## Why this bridge declares no resources

Protocol v0.3 added resources for instrument state that is a tree with
nowhere else to live. An ophyd device has none: its structure is fixed by
its class and is already fully expressed as the descriptor plus scalar
channels, so inventing a resource here would be adding surface to prove a
point. The bridge declares `resources: []`, which costs a signal-shaped
instrument exactly nothing, and that asymmetry is useful evidence about the
primitive itself.

One v0.3 change does reach this bridge: a `CommandAnnotation.safety_class`
of `S3` now genuinely bites. An S3 command requires an operator grant from
a server-side store (`labwire grant`), and a server with S3 commands and no
store refuses to start; see SPEC 8.6 before raising a command's class.

## LIMITATIONS

Read this before believing anything above.

- **No physical hardware, ever.** The bridge is exercised against
  `ophyd.sim` devices and against a pure-Python EPICS soft IOC (caproto) over
  Channel Access. It has never been connected to a real instrument, a
  production IOC, or any beamline, and **no claim is made about NSLS-II, APS,
  Diamond, or any other facility**.
- **Classic synchronous ophyd only.** `ophyd-async` is not supported.
- **Units are validated for presence, not grammar.** The EGU→UCUM table
  covers conventional beamline spellings and is convention-based; it has not
  been checked against a UCUM implementation.
- **Cancellation is best-effort and device-dependent.** Labwire's cancel
  calls `device.stop()` and ends the run immediately; a simulated
  `ophyd.sim` axis ignores `stop()` and still arrives at its target.
- **ophyd is synchronous.** Every call runs in a worker thread, so the server
  keeps answering, but a badly behaved device still occupies a thread.
- **Array- and enum-valued signals are refused**: Labwire v0.2 channels
  carry scalars, and non-numeric channels still need an explicit unit.
- **Move progress is not reported.** `MoveStatus.watch()` yields no useful
  intermediate fractions on simulated devices, so the bridge does not
  fabricate one.

## Licensing

ophyd is a separate BSD-3 project from the Bluesky collaboration (Brookhaven
National Laboratory and contributors), and caproto is BSD-3 as well. Both are
**optional dependencies** here: not vendored, not forked, not modified. See
the repository [`NOTICE`](../../../NOTICE).
