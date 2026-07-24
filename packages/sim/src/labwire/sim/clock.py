"""Time utilities for simulations.

Example:
    >>> from labwire.sim import ScaledClock
    >>> clock = ScaledClock(60.0)  # 1 real second = 60 instrument seconds
"""

import asyncio
from datetime import UTC, datetime, timedelta


class ScaledClock:
    """A :class:`labwire.core.Clock` that runs faster than wall time.

    Lets a physically realistic protocol (a 5-minute dispense, a slow thermal
    ramp) run in milliseconds of test or demo time without faking the physics.

    Example:
        >>> clock = ScaledClock(100.0)
        >>> # await clock.sleep(10)   # returns after 0.1 real seconds
    """

    def __init__(self, scale: float = 1.0) -> None:
        if scale <= 0:
            raise ValueError("scale must be positive")
        self.scale = scale
        self._origin_real = datetime.now(UTC)
        self._elapsed = timedelta()

    def now(self) -> datetime:
        """Current instrument time: origin + scaled elapsed real time."""
        real_elapsed = datetime.now(UTC) - self._origin_real
        return self._origin_real + real_elapsed * self.scale

    async def sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` of instrument time (``seconds / scale`` real)."""
        await asyncio.sleep(seconds / self.scale)
