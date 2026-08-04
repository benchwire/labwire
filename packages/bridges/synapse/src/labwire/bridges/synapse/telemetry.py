"""Reduce a 30 kHz Synapse tap to something a protocol can carry.

This is the central strain of bridging Synapse, and it is a rate problem, not
a modelling one. A ``BroadbandSource`` publishes **one ZeroMQ message per
sample instant**: at 30 kHz that is thirty thousand messages a second, each a
serialized ``synapse.BroadbandFrame`` holding one value per channel. Measured
against the shipped simulator at 30 kHz with four channels, this module
received 59,588 frames in 2.0 s (29,794 frames/s), which is the real shape of
the firehose.

Labwire channels are per-sample JSON-RPC notifications with sequence numbers
and per-subscription rate limits. Putting a 30 kHz raw stream through them
would be a mistake at every layer, so this module does not try. A worker
thread drains the tap and keeps counters; the asyncio side publishes derived
channels once per window, and every one of them is named for what it actually
is:

- ``samples_received``: frames **this bridge** received, not frames the device
  produced. ZeroMQ PUB/SUB drops under back pressure.
- ``frames_dropped``: gaps in the tap's own ``sequence_number``, so the
  reduction's lossiness is visible instead of hidden.
- ``sample_rate_measured_hz``: arrival rate, which is below the configured
  rate exactly when frames are being dropped.
- ``rms_counts``: RMS of ``frame_data`` in **ADC counts**, because that is
  what is on the wire.

There is no microvolt channel here. Converting counts to microvolts needs
``lsb_uV``, which lives in ``NodeStatus.broadband_source.status.electrode``
and travels over a different transport (gRPC ``Info``). When the device does
not report it, the bridge publishes counts and says the microvolt channel is
unavailable rather than inventing a scale factor.

Example:
    >>> from labwire.bridges.synapse.telemetry import TapWindow
    >>> TapWindow(frames=0, dropped=0, elapsed_s=1.0).rate_hz
    0.0
"""

import importlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from labwire.bridges.synapse.client import protos

RECV_TIMEOUT_MS = 200
"""How long the worker blocks on one receive before checking for a stop."""


@dataclass(frozen=True)
class TapWindow:
    """What arrived on the tap during one publication window.

    Example:
        >>> TapWindow(frames=300, dropped=0, elapsed_s=0.01).rate_hz
        30000.0
    """

    frames: int
    dropped: int
    elapsed_s: float
    sum_squares: float = 0.0
    values: int = 0
    error: str | None = None

    @property
    def rate_hz(self) -> float:
        """Frames per second received during the window, 0.0 for an empty one.

        Example:
            >>> TapWindow(frames=10, dropped=0, elapsed_s=2.0).rate_hz
            5.0
        """
        return self.frames / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def rms_counts(self) -> float | None:
        """RMS of the received sample values in ADC counts, or None if empty.

        Example:
            >>> TapWindow(frames=1, dropped=0, elapsed_s=1.0,
            ...           sum_squares=16.0, values=1).rms_counts
            4.0
        """
        if self.values <= 0:
            return None
        return math.sqrt(self.sum_squares / self.values)


class TapReducer:
    """Drain one ZeroMQ tap in a worker thread and keep window counters.

    The worker does no protocol work at all: it receives, parses, and adds to
    integers under a lock. Everything Labwire-shaped happens on the event loop
    when :meth:`take` is called, which is what keeps a 30 kHz stream from
    reaching the protocol layer even once.

    Example:
        >>> # reducer = TapReducer("tcp://127.0.0.1:5555")
        >>> # reducer.start(); window = reducer.take(); reducer.stop()
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames = 0
        self._dropped = 0
        self._sum_squares = 0.0
        self._values = 0
        self._last_seq: int | None = None
        self._since = time.monotonic()
        self._error: str | None = None

    def start(self) -> None:
        """Connect to the tap and begin draining it.

        Example:
            >>> # reducer.start()
        """
        if self._thread is not None:
            return
        self._stop.clear()
        self._since = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name=f"synapse-tap-{self.endpoint}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the worker to finish and wait briefly for it.

        Example:
            >>> # reducer.stop()
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        """Whether the worker thread is alive.

        Example:
            >>> # reducer.running
        """
        return self._thread is not None and self._thread.is_alive()

    def take(self) -> TapWindow:
        """Snapshot the counters and reset them for the next window.

        Example:
            >>> # reducer.take().frames
        """
        now = time.monotonic()
        with self._lock:
            window = TapWindow(
                frames=self._frames,
                dropped=self._dropped,
                elapsed_s=max(now - self._since, 1e-9),
                sum_squares=self._sum_squares,
                values=self._values,
                error=self._error,
            )
            self._frames = 0
            self._dropped = 0
            self._sum_squares = 0.0
            self._values = 0
            self._since = now
        return window

    def _run(self) -> None:
        try:
            zmq: Any = importlib.import_module("zmq")
        except ImportError as exc:  # pragma: no cover - pyzmq ships with the client
            with self._lock:
                self._error = f"pyzmq is not installed, so the tap cannot be read: {exc}"
            return
        frame_type = protos().BroadbandFrame
        context: Any = zmq.Context()
        socket: Any = context.socket(zmq.SUB)
        try:
            socket.setsockopt(zmq.SUBSCRIBE, b"")
            socket.setsockopt(zmq.RCVTIMEO, RECV_TIMEOUT_MS)
            socket.connect(self.endpoint)
            self._drain(socket, zmq, frame_type)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            socket.close(linger=0)
            context.term()

    def _drain(self, socket: Any, zmq: Any, frame_type: Any) -> None:
        frame = frame_type()
        while not self._stop.is_set():
            try:
                raw = socket.recv()
            except zmq.Again:
                continue
            frame.Clear()
            try:
                frame.ParseFromString(raw)
            except Exception:
                with self._lock:
                    self._frames += 1
                continue
            sequence = int(frame.sequence_number)
            data = [float(value) for value in frame.frame_data]
            with self._lock:
                self._frames += 1
                if self._last_seq is not None and sequence > self._last_seq + 1:
                    self._dropped += sequence - self._last_seq - 1
                self._last_seq = sequence
                self._values += len(data)
                self._sum_squares += sum(value * value for value in data)
