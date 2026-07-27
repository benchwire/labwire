# labwire-sim

Simulated laboratory instruments for the Labwire reference implementation.
Each simulator is a first-class device model with latency, noise, drift,
failure modes, and safety interlocks, listening on localhost TCP and speaking an
invented but realistic native wire protocol (SCPI-style for the power supply,
serial-style line protocols for the pump and balance).

These are original device models by the Labwire project. They are **not**
emulations of any real vendor's instrument, and no compatibility with real
hardware is claimed.
