import random
from collections import Counter
from threading import Lock


class FaultInjector:
    """Decides 429/500 from (seed, platform, request number), so runs are reproducible."""

    def __init__(self, enabled: bool, seed: int, rate_429: float, rate_500: float) -> None:
        self._enabled = enabled
        self._seed = seed
        self._rate_429 = rate_429
        self._rate_500 = rate_500
        self._requests: Counter[str] = Counter()
        self._lock = Lock()

    def next_status(self, platform: str) -> int | None:
        """Return the fault status for this platform's next request, or None for no fault."""
        if not self._enabled:
            return None
        with self._lock:
            self._requests[platform] += 1
            request_number = self._requests[platform]
        roll = random.Random(f"{self._seed}:{platform}:{request_number}").random()
        if roll < self._rate_429:
            return 429
        if roll < self._rate_429 + self._rate_500:
            return 500
        return None
