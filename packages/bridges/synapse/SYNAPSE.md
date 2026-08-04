# Synapse: compatibility assessment

**Status: EXPERIMENTAL, branch `synapse-bridge` only. Nothing here is
published to PyPI.** Everything below was measured against the `synapse-sim`
simulator that ships with `science-synapse`, on a laptop and in CI, with no
hardware in the loop at any point. Nothing in this document, this package, or
its tests is evidence about how a Science Corp device behaves. This bridge is
for research rigs. It is not for clinical or implanted use, and it must not be
used as if it were.

Pinned upstream state this assessment was written against:

- `science-synapse` **2.7.6** from PyPI (import name `synapse`), Apache-2.0,
  verified from the `LICENSE` in the wheel's `dist-info`.
- `synapse.SYNAPSE_API_VERSION` reports **2.4.1**, which is the protobuf API
  generation the wheel carries.
- Python 3.12; simulator started as
  `python -m synapse.simulator --iface-ip 127.0.0.1 --rpc-port <ephemeral>`.
- `synapse-api` (the protobuf repository the client is generated from) is
  reported to ship with no `LICENSE` file and a `COPYRIGHT` reserving all
  rights. **TODO-VERIFY**: that was not re-checked from this machine. What was
  verified is that the `science-synapse` distribution this bridge depends on
  carries Apache-2.0, and that this bridge vendors no protobuf definitions.

## What Synapse is

A gRPC control plane plus an out-of-band data plane for neural interface
devices. One service, `synapse.SynapseDevice`, on port 647 by default, with 15
RPCs of which the Python client exposes `Info`, `Configure`, `Start`, `Stop`,
`Query`, `StreamQuery`, `GetLogs`, `TailLogs`, `ListApps` and
`UpdateDeviceSettings`.

A device runs one **signal chain**: a `DeviceConfiguration` holding
`NodeConfig` nodes and `NodeConnection` edges. Node types include
`kBroadbandSource`, `kElectricalStimulation`, `kOpticalStimulation`,
`kSpikeDetector`, `kSpikeSource`, `kSpectralFilter`, `kDiskWriter`,
`kSpikeBinner`, `kApplication`, `kCamera`.

Data does not come back over gRPC. `Query(kListTaps)` returns ZeroMQ endpoints
("taps"), and the client subscribes to those directly.

## What the bridge maps

| Synapse | Labwire |
|---|---|
| One device | One instrument (`max_concurrent_commands = 1`) |
| `Info()` name, serial, `firmware_version`, packed `synapse_version` | `IdentityInfo` |
| `Info()` state, peripherals, chain, connections, power, storage | The `labwire:device` resource |
| `Configure(DeviceConfiguration)` | Six `configure_*` commands over a bridge-held chain |
| `Start()` / `Stop()` | `start_acquisition` (S1), `start_stimulation` (S3), `apply_chain_and_start` (S1), `stop` (S0) |
| `Query(kListTaps / kImpedance / kSelfTest / kGetSettings)` | `list_taps`, `measure_impedance` (S2), `self_test`, `get_settings` |
| `DeviceState.kError` | The `device_error` interlock |
| ZeroMQ `BroadbandFrame` taps | Four derived telemetry channels |

Node ids and connections are assigned by the bridge: processing nodes are
connected in series in the order `broadband_source, spectral_filter,
spike_detector, spike_binner`, and stimulation nodes are added unconnected.
That ordering is this bridge's convention, not a Synapse rule, and it is
stated in the code that implements it.

Not mapped: `DiskWriter`, `Camera`, `Application`, `SpikeSource`, `DeployApp`,
the file RPCs, `GetLogs`/`TailLogs`, `UpdateDeviceSettings`, `StreamQuery`,
and `Query(kSample)`. Two of those omissions are decisions rather than scope:
`SpikeSource` because the shipped simulator's implementation is broken (strain
13), and `kSample` because its response is a bare `repeated uint32 data` with
nothing in the protobuf saying what the numbers are or what unit they are in.
There is no honest UCUM code to declare on it, and SPEC §7.2 does not permit
declaring a quantity without one, so the command does not exist. That is the
units discipline doing exactly what it is for: an unlabelled number does not
get through.

## What Labwire adds that Synapse does not have

Every item here is something Synapse has no representation for at all, not
something it does differently.

1. **Units.** `BroadbandSourceConfig.sample_rate_hz` is a `uint32` whose name
   is the only hint of its dimension; `Thresholder.threshold_uV` is a `uint32`;
   `SpectralFilterConfig.low_cutoff_hz` is a bare float. Through this bridge
   every one of them carries a UCUM code that an agent can read before it
   chooses a value, and every numeric result field carries one too.
2. **A risk class on every command.** Synapse has no notion of risk. Here,
   reading is S0 or S1, impedance measurement is S2 because it injects a test
   current, and anything that can drive tissue is S3.
3. **A confirmation gate**, on `measure_impedance`.
4. **An operator-grant gate**, on `configure_optical_stimulation`,
   `configure_electrical_stimulation` and `start_stimulation`. The grant binds
   the instrument serial, the command name, and the RFC 8785 digest of the
   exact parameter values; it expires; it has a use count; and no protocol
   method can mint one. A confirmation cannot satisfy it.
5. **Declared cancel semantics with honest settlement.** Synapse has no cancel
   or abort RPC of any kind, so fifteen of the sixteen commands declare
   `cancel_semantics: "none"` and the server refuses a cancel against them
   rather than accepting one it cannot honour.
6. **A discovery resource.** `labwire:device` is readable, revisioned, typed,
   and indexed, and it names every field the device declined to report.
7. **Signed evidence.** An ed25519 run manifest records the command, the
   normalized parameters, the settlement, and the digest of any grant spent.

### What it does not add

Safety. `OpticalStimulationConfig` carries a pixel mask, a bit width, a frame
rate, a gain and a receipts flag. `ElectricalStimulationConfig` carries a
peripheral id, channels, a bit width, a sample rate and an LSB. Neither
carries amplitude, pulse width, charge, duty cycle, or any limit at all, so
there is no dose in the protocol for a bridge to bound and this bridge does not
pretend to bound one. The honest claim is exactly this: stimulation through
this bridge is **gated, parameter-bound, and recorded**. It is not made safe,
and no configuration of this software makes a stimulator safe.

## The strains

### 1. Thirty thousand messages a second (the central one)

A `BroadbandSource` publishes **one ZeroMQ message per sample instant**, each a
serialized `BroadbandFrame` holding one `sint32` per channel. Measured against
the simulator at 30 kHz with four channels: **59,588 frames in 2.0 s, 29,794
frames per second**. Labwire channels are per-sample JSON-RPC notifications
with sequence numbers, per-subscription rate limits, and manifest-visible
digests. Thirty thousand of them a second is not a tuning problem, it is a
category error, and the bridge does not attempt it.

The bridge reduces instead. A worker thread owns the ZeroMQ socket, parses each
frame, and adds to integers under a lock; the event loop publishes once per
window (1.0 s by default, 0.2 s in tests). Four channels come out, and each is
named for the thing it is rather than the thing an agent might wish it were:

- `samples_received` (`1`): frames **this bridge received**. ZeroMQ PUB/SUB
  drops under back pressure, so this is not "frames the device produced".
- `frames_dropped` (`1`): gaps in the tap's own `sequence_number`. The
  reduction's own lossiness is published rather than hidden.
- `sample_rate_measured_hz` (`Hz`): arrival rate, which falls below the
  configured rate exactly when frames are being dropped.
- `rms_counts` (`1`): RMS of `frame_data` in raw ADC counts.

Measured, one 0.5 s window through the protocol: **14,674 tap frames in,
four published samples out**, `frames_dropped` 0, `sample_rate_measured_hz`
29,782, `rms_counts` 2363.9. That last number is a check on the arithmetic as
well as the plumbing: the simulator fills 12-bit samples uniformly over
0..4095, whose RMS is `sqrt(4095^2 / 3) = 2364.3`.

The parse cost is real: at 30 kHz the worker thread holds the GIL for a
meaningful fraction of every second, which is a cost this design pays
deliberately so that `frames_dropped` can be honest. A production deployment
would sample rather than parse every frame, and would then have to say so.

**Generalizes past Synapse.** Any instrument whose native data plane is
kilohertz-rate raw samples needs a declared reduction layer, and the protocol
has nothing to say about one today. The channel is where the reduction's
meaning has to live, and here it lives in four channel names and their
descriptions, which is a convention rather than a contract.

### 2. ADC counts, and a scale factor on a different transport

`BroadbandFrame.frame_data` is `sint32` ADC counts. Converting to microvolts
needs `lsb_uV`, which lives at
`NodeStatus.broadband_source.status.electrode.lsb_uV`: four levels down a
status message reached over **gRPC `Info()`**, not over the tap the samples
came from. The simulator never populates `status.signal_chain` at all, so
`lsb_uV` is never available against it.

The bridge's answer is to declare the `rms_uV` channel **only** when the device
reported a scale at construction time, and otherwise not to declare it. A
channel that could never produce a sample is a false advertisement in the
descriptor; a microvolt figure computed from a scale nobody reported would be
worse. Against the simulator the descriptor therefore has four telemetry
channels, not five, and `labwire:device` says
`microvolt_scale_available: false` with `lsb_uV` named in `unavailable`.

Consequence worth stating plainly: the descriptor's channel list depends on
device state at startup. If a device begins reporting `lsb_uV` later, the
channel does not appear until the server is restarted.

### 3. There is no safety vocabulary to inherit

Synapse has no safety classification, no confirmation, no authorization, no
e-stop, no interlock concept, and no limits on the stimulation node configs.
Everything in the S0-S3 column of this bridge is a judgement made here, by this
bridge, on this side of the boundary. Two of those judgements are worth
challenging and are recorded so they can be:

- `self_test` is S1. Synapse says nothing about what `kSelfTest` does, and on a
  device whose self-test drives current it is the wrong class. A deployment
  that knows better should say so.
- `measure_impedance` is S2 because an impedance measurement injects a known
  current. That is a fact about impedance measurement, not a fact Synapse
  states anywhere.

### 4. No authentication and no transport security, anywhere

The control plane uses `grpc.insecure_channel` on the client and
`add_insecure_port` on the server. The data plane is plain `tcp://` ZeroMQ with
no authentication. There is no permissions mechanism in the protocol; the
`kPermissionDenied` status code exists with nothing in the API that would
produce it.

Labwire's grant path is an **operator** gate, not a network one. It stops an
agent from stimulating without a human approving those exact parameters. It
does nothing whatsoever about anyone else on the network who can reach port
647. Deployments must treat the device network as the security boundary,
because the protocol supplies none.

### 5. The lifecycle is device-global, and that breaks a static safety class

`Start()` and `Stop()` take `Empty` and act on the whole device. There is no
per-node start, no per-node stop, and no way to run an acquisition chain while
leaving a configured stimulator idle.

That means the S3 gate on `configure_optical_stimulation` would be bypassable:
an agent could pass the gate once to install the node, and then energize it
with an S1 `start_acquisition`. The bridge closes this by splitting the start:

- `start_acquisition` (S1) refuses outright if the installed or pending chain
  contains a stimulation node, and names `start_stimulation` in the refusal.
- `start_stimulation` (S3) is the call that energizes, needs its own grant, and
  refuses when no stimulation node is installed so it cannot be used as an
  unclassified way to start an ordinary acquisition.

This is a bridge-level fix for a protocol-level gap. See the proposed finding
below.

### 6. Configure replaces everything, is not atomic, and a rejection is destructive

`Configure(DeviceConfiguration)` replaces the entire chain every time. There is
no add-node, no remove-node, no per-node update. A bridge exposing
`configure_broadband` and `configure_filter` as independent commands would have
the second silently delete the first, so this bridge holds the chain it means
to have and sends the whole thing on every edit.

Three measured consequences:

- **A rejected Configure is not atomic.** Sending
  `[broadband_source, spike_detector]` to the simulator, which does not
  implement spike detectors, installs the broadband source and *then* answers
  `kUndefinedError: Failed to configure`. The device is left holding a prefix
  of what was sent. A test pins this.
- **A rejected Configure is destructive.** Sending a chain of one unsupported
  node leaves the device with an empty chain and state `kInitializing`. The
  bridge re-reads `Info()` on the way out of the failure so the resource
  describes what is there rather than what was asked for.
- **`kOk` is not evidence the chain is installed.** The simulator drops a node
  whose own `configure()` fails, warns to its log, and still answers `kOk`. So
  the bridge reads the chain back after every configure and refuses with a
  hardware fault if what came back is not what went out. A test proves the
  guard using a device stand-in whose `Configure` claims success and does
  nothing.

The bridge commits its own chain only after the read-back matches, so a
rejected node never poisons later commands, and `labwire:device` publishes both
`nodes` (what the device reports) and `pending_chain` (what the bridge holds)
so a divergence is visible rather than inferred.

### 7. The status vocabulary is coarser in practice than on paper

`StatusCode` has `kInvalidConfiguration`, `kFailedPrecondition`,
`kUnimplemented`, `kInternalError`, `kPermissionDenied` and `kQueryFailed`. The
shipped simulator uses exactly one of them: `kUndefinedError`, for "Device is
not running" (a precondition), "Failed to configure" (an invalid
configuration), and "Failed to stop streaming" (also a precondition).

The bridge maps `kUndefinedError` to `HardwareFaultError`, which is the
conservative choice and the wrong one for two of those three cases. There is no
honest way to do better without parsing status message strings, which would be
worse. Where the mismatch mattered most, the bridge routes around it instead:
`stop` reads the state first and reports "was not running" rather than issuing
a Stop the device would answer with an error, because `stop` is the S0 recovery
path and must not fail for being redundant.

### 8. The Python client swallows every gRPC error

Every method of `synapse.client.device.Device` catches `grpc.RpcError`, writes
it to a logger, and returns `None` or `False`. That includes the
`*_with_status` variants, which are otherwise the documented way to see a
device's own error text. A caller cannot distinguish "device unreachable" from
"device answered with nothing".

The bridge attaches a `logging.Handler` to `synapse.client.device` and reads the
detail back off it (`ClientErrorCapture`), so an agent asking an unreachable
device for `Info()` is told "failed to connect to all addresses ... Connection
refused" instead of "no response". It is a workaround, it is recorded as one,
and a test proves it recovers the real text. Missing responses are reported as
`DeviceTimeoutError`, because "the instrument did not respond" is what actually
happened.

### 9. `synapse.client.Config` shares mutable state across instances

`Config.nodes` and `Config.connections` are **class** attributes, mutated in
place by `add_node()` and `connect()`. A freshly constructed `Config()` already
contains every node any earlier `Config` in the process was given. Verified
directly: after building one `Config` with a filter node, a second, brand new
`Config()` reports one node, and `a.nodes is b.nodes` is `True`.

Any long-lived process that configures a device more than once will send a
chain it did not build. The bridge does not use `Config` at all.

### 10. The `ElectricalStimulation` client wrapper cannot serialize itself

`synapse/client/nodes/electrical_stimulation.py` does
`channels = [c.to_proto() for c in self.channels]`, where the `Channel` its own
sibling module exports is `synapse.api.channel_pb2.Channel`, a protobuf message
with no `to_proto` method. The reverse direction calls `Channel.from_proto`,
which also does not exist. The node is unconstructible through the client in
either direction.

Together with strain 9, this is why the bridge builds `DeviceConfiguration`
protos itself and sends them through the generated stub
(`device.rpc.Configure`) rather than through `configure_with_status(Config)`.
That also has the merit of raising real `grpc.RpcError` instead of hiding it.
The other four RPCs go through the documented `Device` methods.

### 11. The simulator answers every query the same way

`SynapseServicer.Query` ignores `request.query_type` entirely (there is a
literal `# handle query` comment where the dispatch would be) and returns
`data=[1,2,3,4,5]` plus the tap list, for every query type. So `kImpedance`,
`kSelfTest` and `kGetSettings` come back with no payload of their own.

The bridge refuses rather than fabricating: `measure_impedance` raises
`UnsupportedError` saying the device answered without an impedance payload and
that this bridge will not invent one. Returning `count: 0` with an empty list
would have been readable as "all electrodes measured, none found", which is a
different and false statement. The same applies to `self_test` and
`get_settings`.

**The consequence for verification is real:** the impedance, self-test and
settings paths have never returned a payload in any test, on any device. Their
parsing code has never run against real data. This is the largest unverified
surface in the bridge and it is listed again below.

### 12. Continuous quantities on `uint32` wire fields

`Thresholder.threshold_uV` is a `uint32`. `SpikeBinnerConfig.bin_size_ms` is a
`uint32`. `BroadbandSourceConfig.sample_rate_hz` is a `uint32`. The parameters
are declared here in `uV`, `ms` and `Hz`, which are continuous quantities, and
the wire cannot carry a fraction of one.

Truncating silently would make the declared unit a lie about the value that was
sent, which is finding F9 with a specific mechanism. The bridge refuses instead:
`50.5 uV` is rejected with a message naming the field, the unit, and the reason.
An agent that wanted 50.5 uV learns that it cannot have it, rather than getting
50 and being told it got what it asked for.

### 13. `SpikeSource` is broken in the shipped simulator, so it is not exposed

`synapse/simulator/nodes/spike_source.py` calls `c.HasField("signal")` on a
`SpikeSourceConfig`, which has no `signal` field (its electrode config is
called `electrodes`). Verified: `configure` succeeds and `Start` succeeds, and
the node's `run()` coroutine then fails in the background where neither the
client nor the bridge can see it, so the device reports `kRunning` and produces
nothing.

The bridge does not expose spike-source configuration. A command that reliably
appears to work while producing no data is worse than a missing command.

### 14. The version fields do not say what they look like they say

`DeviceInfo.firmware_version` is a `uint32`, not a version string; the
simulator hardcodes `1`. `DeviceInfo.synapse_version` is a packed integer,
`(major & 0x3FF) << 20 | (minor & 0x3FF) << 10 | (patch & 0x3FF)`, and the
simulator reports **0**, because the server reads it from an `api/version.txt`
that the wheel does not contain.

The bridge unpacks the packing and renders `0` as `"unreported"` rather than
`"0.0.0"`, and names it in `unavailable`. `"0.0.0"` would have been a version
number, and there was no version number.

### 15. There is no cancel path to be honest about

No `Cancel`, no `Abort`, no per-node stop, no deadline in the client's calls.
Every RPC the bridge issues is committed the moment it is issued.

Fifteen of the sixteen commands therefore declare `cancel_semantics: "none"`,
which means the server refuses a cancel against a running one with `-32007`
rather than accepting it and doing nothing. The one exception is
`apply_chain_and_start`, which the bridge sequences itself: `Configure`, then a
boundary, then `Start`. A cancel accepted while the configure is in flight lets
that configure finish and stops before `Start` is issued, and the settlement
record says `halted_at_boundary` with `last: "configure"`,
`completed_steps: 1`, `of_steps: 2`. A test proves it, and proves that `Start`
was never issued and the device is left configured and stopped. Neither step is
itself interruptible, and the command says so.

### 16. Dependency weight and an exact pin

`science-synapse` 2.7.6 requires `dearpygui`, `pyqt5`, `pyqtgraph`, `pandas`,
`scipy`, `h5py`, `paramiko`, `numexpr`, `protoletariat` and `grpcio-tools` to
talk to a device, and pins `rich==14.0.0` exactly. The exact pin is the one
that bites: installing it into this workspace downgraded `rich` from 15.0.0.
This is why `science-synapse` is an extra in no dependency group, why normal CI
never installs it, and why the branch-local job runs only this package's tests.

## A finding Labwire's spec does not currently cover

Written up here in SPEC-FINDINGS style. It is **not** written into the root
`SPEC-FINDINGS.md` on this branch; the next free number there is F12, and
whether this earns it is a call for the spec, not for a bridge.

### Proposed F12: a static safety class cannot cover a hazard a previous command installed

**Severity:** significant. **Status:** open, worked around at the bridge level.

**Sibling of F3, not a duplicate.** F3 is "safety class is static when the risk
is in the arguments": `dispense(1 uL)` and `dispense(10 L)` share a class
because the class sits on the command. This is the same stiffness one level
further out: the risk is not in the arguments of the command being submitted at
all. It is in **device state that an earlier, separately classified command
installed**.

Synapse is a clean instance. `Start()` takes no arguments and acts on the whole
device. Its hazard is entirely determined by what is in the signal chain, which
was decided by a different command, possibly in a different session, possibly by
a different agent. `start_acquisition` on a chain of a broadband source and a
filter is as routine as a command gets. The same call on a chain containing an
optical stimulator delivers light into tissue. One command, one declared class,
two categorically different acts.

The protocol has no way to express "this command is S1 except when resource
`labwire:device` reports a stimulation node, in which case it is S3". The
descriptor is static; `safety_class` is a field on `CommandSpec`; and SPEC §8.6
requires servers not to downgrade a declared class, which correctly forbids the
dangerous direction but says nothing about the case where the class should be
*raised* by state.

**What this bridge did instead**, which is the only thing available today:
split the command in two and refuse. `start_acquisition` is S1 and refuses
outright when the chain contains a stimulation node; `start_stimulation` is S3
and refuses when it does not. The refusal names the other command. This works,
it is testable, and both tests are in the suite. Its cost is a command surface
shaped by the safety model rather than by the instrument, and it does not
generalize: a device with four hazard-bearing node types would need a start
command per combination.

**Directions the spec could take**, none of them free:

1. A declared **precondition class**: a command carries a base class plus a
   rule naming a resource and a predicate that raises it. Turns the descriptor
   into a small language and needs the predicate evaluated server-side against
   a fresh read, which is the same machinery `resource_ref` validation already
   has (SPEC §10.4).
2. **State-bound grants**: let a grant bind a resource revision as well as a
   params digest, so authorizing "start with this exact chain installed"
   becomes expressible. Cheaper, and it composes with `if_revision`, but it
   only helps commands that are already S3.
3. **Accept the split** and say so in the spec: when a hazard lives in state,
   the instrument must expose separate commands per hazard level, and the
   safe-named one must refuse rather than proceed. That is what this bridge
   does, and writing it down at least makes it a pattern rather than an
   accident.

**A smaller, related observation**, not worth a finding on its own: resource
content is serialized with `exclude_none`, so an optional quantity the device
did not report is absent from `content` rather than null. Absence is then
ambiguous between "the device did not say" and "this field is not in the
schema". This bridge answers with an explicit `unavailable` list naming every
withheld field, which works but is per-instrument convention where a protocol
convention would serve better.

## What was verified, and what was not

**Verified end to end against the simulator**, through a real
`InstrumentServer` and `LabwireClient` over `MemoryTransport`, 23 tests:

- Descriptor shape: command set, safety classes, cancel semantics, unit
  annotations, return units, channels, resource declaration, interlock.
- Identity derived from `Info()`, including the manufacturer disclaimer.
- The `labwire:device` projection, its index, its revision changing, and its
  `unavailable` list.
- `configure_broadband` + `configure_filter` + `start_acquisition` +
  `stop`, with the chain and connections read back from the device.
- A live 30 kHz tap reduced to derived channels, subscribed through the
  protocol.
- Configure atomicity, destructiveness, and the `kOk`-but-nothing-installed
  guard.
- Error mapping, including a query against a stopped device and a Stop against
  a stopped device.
- The swallowed-gRPC-error recovery, against a device that is not there.
- The S2 confirmation gate on impedance and the honest refusal behind it.
- The S3 grant path: refusal without a grant, refusal with a confirmation
  instead of a grant, success with a grant, exhaustion, parameter mismatch, and
  the separate grant needed to energize.
- The S1 start refusing to energize a configured stimulator.
- The interlock keeping S0 commands submittable while S1 is refused.
- Cancellation settling at the boundary of the one sequenced command.
- Fractional values on uint32 fields being refused rather than truncated.

**Not verified, and not verifiable without hardware:**

- Any behaviour of any Science Corp device. All of it.
- `measure_impedance`, `self_test` and `get_settings` payload parsing. The
  simulator never returns those payloads, so that code has never run against
  real data. The unit codes on `magnitude` (`Ohm`) and `phase` (`deg`) are
  this bridge's reading of `ImpedanceMeasurement`; the protobuf does not label
  them.
- `lsb_uV` and therefore the entire microvolt path, including whether the
  `rms_uV` channel is declared correctly when a scale is present.
- `kElectricalStimulation` in any working form. The simulator refuses the node
  type, so only the grant gate and the honest refusal are proven.
- `kSpikeDetector` and `kSpikeBinner` configuration. The simulator refuses both
  node types, so only the refusal path and the fractional-value guard are
  proven.
- Peripherals, power and storage projection. The simulator reports none of
  them, so those branches of the projection have never run with data.
- Whether `Stop()` returning `kOk` corresponds to emission actually ceasing.
  Synapse offers no confirmation of physical state, and this is exactly the
  situation finding F10 was written about.
- Whether ZeroMQ frame loss behaves the same on a real device's network as it
  does over loopback. `frames_dropped` has only ever been observed at zero.

## What is shaky

- The `_lsb_uv` read walks four levels of a status message with `getattr` and
  attribute access on a protobuf oneof. It returns `None` for anything it
  cannot reach, which is the right failure, but it has never reached a value.
- The telemetry worker parses every frame. At 30 kHz that is a real CPU cost
  paid on the same machine as the event loop, and the only evidence it is
  survivable is one laptop and one CI runner.
- The chain model allows at most one node of each kind. Synapse allows several.
  A device with two broadband sources cannot be driven through this bridge.
- `start_acquisition`'s refusal checks the installed chain from a fresh `Info()`
  and the bridge's pending chain. If a device grew a stimulation node by some
  path this bridge does not know about between that read and the `Start`, the
  refusal would not fire. There is no atomic read-and-start in Synapse.
- The simulator's readiness check in the test fixture binds an ephemeral port
  and releases it before the subprocess claims it. It is the usual race, and it
  is small, but it is there. The simulator's own entrypoint also binds UDP port
  8000 briefly at startup to validate the interface, which is a fixed port this
  bridge does not control.

## What would have to change upstream

1. A per-node lifecycle, or any way to run acquisition while leaving a
   configured stimulator idle. Everything in strain 5 and proposed F12 follows
   from its absence.
2. Amplitude, charge, or duty limits in the stimulation node configs, so there
   is a dose for a protocol to bound rather than only an act to gate.
3. `lsb_uV` on the tap, or in the tap's message, so samples carry their own
   scale instead of needing a second transport to interpret.
4. A client that raises. `*_with_status` returning `None` on transport failure
   is the single largest source of workaround code in this bridge.
5. `Config` instance state, and an `ElectricalStimulation` wrapper that can
   serialize. Both are ordinary bugs.
6. Any authentication at all, on either plane.
