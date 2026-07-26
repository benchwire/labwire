# labwire-ophyd

Expose any classic (synchronous) [ophyd](https://github.com/bluesky/ophyd)
`Device` as a Labwire instrument, so AI agents can discover and drive the
large existing body of Python instrument drivers through the Labwire
protocol.

Ophyd abstracts hardware *for Python programmers*; Labwire describes
hardware *to AI agents*. This bridge makes them compose — Labwire does not
reimplement drivers.

**Status: under construction.** Full documentation and quickstart land with
milestone B6. See [DESIGN.md](DESIGN.md) for the mapping model and its
rationale, including the honest limitations.

The bridge is tested against `ophyd.sim` devices and against a real EPICS
soft IOC (caproto) over Channel Access — which is how unit adoption from
`.EGU` metadata is verified. No physical hardware or beamline has been
involved.

ophyd is an optional dependency and is neither vendored nor modified here;
it is BSD-3 licensed (see the repository `NOTICE`).
